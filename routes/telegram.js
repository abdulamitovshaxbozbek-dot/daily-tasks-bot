const express = require('express');
const router = express.Router();

const pool = require('../db');
const TelegramAPI = require('node-telegram-bot-api');

const bot = new TelegramAPI(process.env.TELEGRAM_TOKEN);

const TIMEZONE = 'Asia/Tashkent';

/* =========================================================
   YORDAMCHI FUNKSIYALAR
========================================================= */

function getToday() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: TIMEZONE
  }).format(new Date());
}

function getDateParts(dateString = getToday()) {
  const [year, month, day] = dateString.split('-').map(Number);

  return {
    year,
    month,
    day
  };
}

function normalizeTask(text) {
  return String(text)
    .trim()
    .toLowerCase()
    .replace(/\s+/g, ' ');
}

function cleanTaskLines(text) {
  return String(text)
    .split('\n')
    .map(line => line.trim())
    .map(line => line.replace(/^\d+[\.\)\-]\s*/, ''))
    .filter(Boolean)
    .filter(line => !line.startsWith('/'));
}

function getMorningKeyboard() {
  return {
    inline_keyboard: [
      [
        { text: '02:00', callback_data: 'morning_time|02:00' },
        { text: '03:00', callback_data: 'morning_time|03:00' },
        { text: '04:00', callback_data: 'morning_time|04:00' }
      ],
      [
        { text: '05:00', callback_data: 'morning_time|05:00' },
        { text: '06:00', callback_data: 'morning_time|06:00' },
        { text: '07:00', callback_data: 'morning_time|07:00' }
      ],
      [
        { text: '08:00', callback_data: 'morning_time|08:00' },
        { text: '09:00', callback_data: 'morning_time|09:00' },
        { text: '10:00', callback_data: 'morning_time|10:00' }
      ]
    ]
  };
}

function getStats(tasks) {
  const total = tasks.length;

  const completed = tasks.filter(
    task => task.status === 'completed'
  ).length;

  const failed = tasks.filter(
    task => task.status === 'failed'
  ).length;

  const pending = tasks.filter(
    task => task.status === 'pending'
  ).length;

  const percent =
    total > 0
      ? Math.round((completed / total) * 100)
      : 0;

  return {
    total,
    completed,
    failed,
    pending,
    percent
  };
}

function progressBar(percent) {
  const blocks = 10;
  const filled = Math.round(percent / 100 * blocks);

  return (
    '🟩'.repeat(filled) +
    '⬜'.repeat(blocks - filled)
  );
}

async function getUser(chatId) {
  const result = await pool.query(
    `
    SELECT *
    FROM users
    WHERE telegram_chat_id = $1
    LIMIT 1
    `,
    [chatId]
  );

  return result.rows[0] || null;
}

async function requireUser(chatId) {
  const user = await getUser(chatId);

  if (!user) {
    await bot.sendMessage(
      chatId,
      `⚠️ Avval /start buyrug'ini bering.`
    );

    return null;
  }

  return user;
}

/* =========================================================
   /START
========================================================= */

async function handleStart(chatId, firstName, username) {
  let user = await getUser(chatId);

  if (!user) {
    const result = await pool.query(
      `
      INSERT INTO users (
        telegram_chat_id,
        telegram_username,
        first_name,
        timezone,
        state,
        subscription_status
      )
      VALUES (
        $1,
        $2,
        $3,
        $4,
        'waiting_morning_time',
        'trial'
      )
      RETURNING *
      `,
      [
        chatId,
        username || null,
        firstName,
        TIMEZONE
      ]
    );

    user = result.rows[0];
  }

  if (
    user.state === 'waiting_morning_time' ||
    !user.morning_time
  ) {
    await bot.sendMessage(
      chatId,
      `👋 Assalomu alaykum, ${firstName}!

📋 Kunlik vazifalar botiga xush kelibsiz.

🌅 Ertalab qaysi vaqtda vazifalaringizni so'raylik?`,
      {
        reply_markup: getMorningKeyboard()
      }
    );

    return;
  }

  await bot.sendMessage(
    chatId,
    `👋 Assalomu alaykum, ${firstName}!

Siz allaqachon ro'yxatdan o'tgansiz. ✅

📋 Vazifalaringizni yuborishni davom ettirishingiz mumkin.

Masalan:

kitob o'qish
yozish
chizish

📊 Hisobot uchun:
/hisobot
/haftalik
/oylik
/yillik`
  );
}

/* =========================================================
   ERTALABKI VAQT
========================================================= */

async function handleMorningTime(
  chatId,
  callbackData,
  callbackQueryId
) {
  const [, time] = callbackData.split('|');

  if (!time) {
    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Vaqt topilmadi.'
      }
    );

    return;
  }

  await pool.query(
    `
    UPDATE users
    SET
      morning_time = $1,
      timezone = $2,
      state = 'active'
    WHERE telegram_chat_id = $3
    `,
    [
      time,
      TIMEZONE,
      chatId
    ]
  );

  await bot.answerCallbackQuery(
    callbackQueryId,
    {
      text: 'Vaqt saqlandi! ✅'
    }
  );

  await bot.sendMessage(
    chatId,
    `✅ Tayyor!

🌅 Ertalabki vaqt: ${time}

Endi kunlik vazifalaringizni yuborishingiz mumkin.

Masalan:

kitob o'qish
yozish
chizish`
  );
}

/* =========================================================
   VAZIFA YOZISH
========================================================= */

async function handleTaskWrite(chatId, text) {
  const user = await requireUser(chatId);

  if (!user) return;

  /*
    Agar user avvalgi kunni completed holatida
    yakunlagan bo'lsa, yangi task yozishni
    yangi kun sifatida davom ettirishga ruxsat.
  */

  if (
    user.state === 'waiting_morning_time' ||
    !user.morning_time
  ) {
    await bot.sendMessage(
      chatId,
      `⚠️ Avval ertalabki vaqtni tanlang.

Buning uchun /start yuboring.`
    );

    return;
  }

  const today = getToday();

  const lines = cleanTaskLines(text);

  if (lines.length === 0) {
    return;
  }

  const existingResult = await pool.query(
    `
    SELECT id, task_text
    FROM tasks
    WHERE user_id = $1
      AND task_date = $2
    `,
    [
      user.id,
      today
    ]
  );

  const existingTasks = existingResult.rows;

  const existingNormalized = new Set(
    existingTasks.map(task =>
      normalizeTask(task.task_text)
    )
  );

  const uniqueInput = [];
  const seenInput = new Set();

  for (const taskText of lines) {
    const normalized = normalizeTask(taskText);

    if (!normalized) continue;

    if (seenInput.has(normalized)) continue;

    seenInput.add(normalized);

    if (existingNormalized.has(normalized)) {
      continue;
    }

    uniqueInput.push(taskText);
  }

  if (uniqueInput.length === 0) {
    await bot.sendMessage(
      chatId,
      `ℹ️ Bu vazifalar bugun allaqachon qo'shilgan.`
    );

    return;
  }

  /*
    Agar user oldin kunni completed qilgan bo'lsa,
    yangi task qo'shilishi bilan active holatga qaytariladi.
  */

  await pool.query(
    `
    UPDATE users
    SET state = 'active'
    WHERE id = $1
    `,
    [user.id]
  );

  const client = await pool.connect();

  let insertedTasks = [];

  try {
    await client.query('BEGIN');

    for (const taskText of uniqueInput) {
      const result = await client.query(
        `
        INSERT INTO tasks (
          user_id,
          task_date,
          task_text,
          status
        )
        VALUES (
          $1,
          $2,
          $3,
          'pending'
        )
        RETURNING *
        `,
        [
          user.id,
          today,
          taskText
        ]
      );

      insertedTasks.push(result.rows[0]);
    }

    await client.query('COMMIT');

  } catch (error) {
    await client.query('ROLLBACK');
    throw error;

  } finally {
    client.release();
  }

  const messageText =
    `📋 *${insertedTasks.length} ta yangi vazifa qo'shildi!*\n\n` +
    insertedTasks
      .map(
        (task, index) =>
          `${index + 1}. ${task.task_text}`
      )
      .join('\n') +
    `\n\n👇 Har bir vazifa uchun holatni belgilang.`;

  /*
    Har bir vazifa uchun:
    ✅ Bajarildi
    ❌ Bajarilmadi
  */

  const keyboard = insertedTasks.map(task => [
    {
      text: `✅ ${task.task_text}`,
      callback_data:
        `task_status|completed|${task.id}`
    },
    {
      text: `❌ Bajarilmadi`,
      callback_data:
        `task_status|failed|${task.id}`
    }
  ]);

  const sentMessage = await bot.sendMessage(
    chatId,
    messageText,
    {
      parse_mode: 'Markdown',
      reply_markup: {
        inline_keyboard: keyboard
      }
    }
  );

  /*
    Telegram message ID ni barcha yangi tasklarga saqlaymiz.
  */

  await pool.query(
    `
    UPDATE tasks
    SET telegram_message_id = $1
    WHERE id = ANY($2::uuid[])
    `,
    [
      sentMessage.message_id,
      insertedTasks.map(task => task.id)
    ]
  );
}

/* =========================================================
   TASK STATUS
   task_status|completed|UUID
   task_status|failed|UUID
========================================================= */

async function handleTaskStatus(
  chatId,
  callbackData,
  callbackQueryId,
  telegramMessage
) {
  const parts = callbackData.split('|');

  const status = parts[1];
  const taskId = parts[2];

  if (
    !['completed', 'failed'].includes(status) ||
    !taskId
  ) {
    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Noto‘g‘ri ma‘lumot.'
      }
    );

    return;
  }

  const user = await requireUser(chatId);

  if (!user) return;

  const result = await pool.query(
    `
    UPDATE tasks
    SET status = $1
    WHERE id = $2
      AND user_id = $3
    RETURNING *
    `,
    [
      status,
      taskId,
      user.id
    ]
  );

  if (result.rows.length === 0) {
    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Vazifa topilmadi.'
      }
    );

    return;
  }

  const task = result.rows[0];

  await bot.answerCallbackQuery(
    callbackQueryId,
    {
      text:
        status === 'completed'
          ? 'Bajarildi! 🎉'
          : 'Bajarilmadi deb belgilandi.'
    }
  );

  /*
    Tugmalarni yangilash.
  */

  if (telegramMessage) {
    try {
      await bot.editMessageReplyMarkup(
        {
          inline_keyboard: [
            [
              {
                text:
                  status === 'completed'
                    ? `✅ Bajarildi: ${task.task_text}`
                    : `❌ Bajarilmadi: ${task.task_text}`,
                callback_data:
                  `task_status|${status}|${task.id}`
              }
            ]
          ]
        },
        {
          chat_id: chatId,
          message_id: telegramMessage.message_id
        }
      );
    } catch (error) {
      console.log(
        'Keyboard update error:',
        error.message
      );
    }
  }

  /*
    Bugungi barcha tasklarni tekshirish.
    Pending qolmagan bo'lsa user completed.
  */

  const today = getToday();

  const statsResult = await pool.query(
    `
    SELECT
      COUNT(*)::int AS total,
      COUNT(*) FILTER (
        WHERE status = 'pending'
      )::int AS pending
    FROM tasks
    WHERE user_id = $1
      AND task_date = $2
    `,
    [
      user.id,
      today
    ]
  );

  const stats = statsResult.rows[0];

  const allDone =
    stats.total > 0 &&
    stats.pending === 0;

  if (allDone) {
    await pool.query(
      `
      UPDATE users
      SET state = 'completed'
      WHERE id = $1
        AND state <> 'completed'
      `,
      [user.id]
    );

    await bot.sendMessage(
      chatId,
      `🏁 Bugungi barcha vazifalar belgilandi!

🎉 Kun yakunlandi.

📊 Natijani ko'rish uchun /hisobot buyrug'ini yuboring.`
    );
  }
}

/* =========================================================
   /YAKUNLADIM
========================================================= */

async function handleFinishDay(chatId) {
  const user = await requireUser(chatId);

  if (!user) return;

  const today = getToday();

  /*
    Belgilanmagan barcha tasklarni failed qilamiz.
  */

  const pendingResult = await pool.query(
    `
    UPDATE tasks
    SET status = 'failed'
    WHERE user_id = $1
      AND task_date = $2
      AND status = 'pending'
    RETURNING task_text
    `,
    [
      user.id,
      today
    ]
  );

  const result = await pool.query(
    `
    SELECT *
    FROM tasks
    WHERE user_id = $1
      AND task_date = $2
    ORDER BY created_at
    `,
    [
      user.id,
      today
    ]
  );

  const tasks = result.rows;

  if (tasks.length === 0) {
    await bot.sendMessage(
      chatId,
      `📭 Bugun uchun vazifalar topilmadi.`
    );

    return;
  }

  await pool.query(
    `
    UPDATE users
    SET state = 'completed'
    WHERE id = $1
    `,
    [user.id]
  );

  const stats = getStats(tasks);

  let text =
    `🏁 *KUN YAKUNLANDI*\n\n` +
    `🎯 Natija: *${stats.percent}%*\n` +
    `${progressBar(stats.percent)}\n\n` +
    `📋 Jami: ${stats.total}\n` +
    `✅ Bajarildi: ${stats.completed}\n` +
    `❌ Bajarilmadi: ${stats.failed}`;

  if (pendingResult.rows.length > 0) {
    text +=
      `\n\n⚠️ ${pendingResult.rows.length} ta belgilanmagan vazifa bajarilmadi deb hisoblandi.`;
  }

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

/* =========================================================
   /HISOBOT
========================================================= */

async function handleDailyReport(chatId) {
  const user = await requireUser(chatId);

  if (!user) return;

  const today = getToday();

  const result = await pool.query(
    `
    SELECT *
    FROM tasks
    WHERE user_id = $1
      AND task_date = $2
    ORDER BY created_at
    `,
    [
      user.id,
      today
    ]
  );

  const tasks = result.rows;

  if (tasks.length === 0) {
    await bot.sendMessage(
      chatId,
      `📭 Bugun uchun vazifalar topilmadi.`
    );

    return;
  }

  const stats = getStats(tasks);

  let text =
    `📊 *BUGUNGI HISOBOT*\n\n` +
    `🎯 Natija: *${stats.percent}%*\n` +
    `${progressBar(stats.percent)}\n\n` +
    `📋 Jami vazifalar: ${stats.total}\n` +
    `✅ Bajarildi: ${stats.completed}\n` +
    `❌ Bajarilmadi: ${stats.failed}`;

  if (stats.pending > 0) {
    text +=
      `\n⏳ Belgilanmagan: ${stats.pending}`;
  }

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

/* =========================================================
   /HAFTALIK
   Original workflow'dagi kabi o'tgan to'liq hafta
   va undan oldingi hafta bilan solishtiriladi.
========================================================= */

async function handleWeeklyReport(chatId) {
  const user = await requireUser(chatId);

  if (!user) return;

  const now = new Date();

  /*
    Tashkent sanasini olish.
  */

  const todayString = getToday();

  const [
    year,
    month,
    day
  ] = todayString.split('-').map(Number);

  const today = new Date(
    Date.UTC(
      year,
      month - 1,
      day,
      12
    )
  );

  const dow = today.getUTCDay();

  const daysSinceMonday =
    (dow + 6) % 7;

  const thisMonday = new Date(today);

  thisMonday.setUTCDate(
    thisMonday.getUTCDate() - daysSinceMonday
  );

  const weekStartDate =
    new Date(thisMonday);

  weekStartDate.setUTCDate(
    weekStartDate.getUTCDate() - 7
  );

  const weekEndDate =
    new Date(thisMonday);

  weekEndDate.setUTCDate(
    weekEndDate.getUTCDate() - 1
  );

  const previousWeekStartDate =
    new Date(weekStartDate);

  previousWeekStartDate.setUTCDate(
    previousWeekStartDate.getUTCDate() - 7
  );

  const previousWeekEndDate =
    new Date(weekEndDate);

  previousWeekEndDate.setUTCDate(
    previousWeekEndDate.getUTCDate() - 7
  );

  const iso = date =>
    date.toISOString().slice(0, 10);

  const weekStart = iso(weekStartDate);
  const weekEnd = iso(weekEndDate);

  const previousWeekStart =
    iso(previousWeekStartDate);

  const previousWeekEnd =
    iso(previousWeekEndDate);

  const result = await pool.query(
    `
    SELECT *
    FROM tasks
    WHERE user_id = $1
      AND task_date >= $2
      AND task_date <= $3
    ORDER BY task_date
    `,
    [
      user.id,
      previousWeekStart,
      weekEnd
    ]
  );

  const allTasks = result.rows;

  const currentTasks =
    allTasks.filter(
      task =>
        task.task_date >= weekStart &&
        task.task_date <= weekEnd
    );

  const previousTasks =
    allTasks.filter(
      task =>
        task.task_date >= previousWeekStart &&
        task.task_date <= previousWeekEnd
    );

  const current = getStats(currentTasks);
  const previous = getStats(previousTasks);

  const dayNames = [
    'Dushanba',
    'Seshanba',
    'Chorshanba',
    'Payshanba',
    'Juma',
    'Shanba',
    'Yakshanba'
  ];

  const byDate = {};

  for (const task of currentTasks) {
    if (!byDate[task.task_date]) {
      byDate[task.task_date] = [];
    }

    byDate[task.task_date].push(task);
  }

  let dailyLines = '';

  for (let i = 0; i < 7; i++) {
    const date = new Date(weekStartDate);

    date.setUTCDate(
      date.getUTCDate() + i
    );

    const dateString = iso(date);

    const tasks = byDate[dateString];

    if (!tasks || tasks.length === 0) {
      dailyLines +=
        `⬜ ${dayNames[i]} — Ma'lumot yo'q\n`;

      continue;
    }

    const stats = getStats(tasks);

    let icon = '🟥';

    if (stats.percent >= 80) {
      icon = '🟩';
    } else if (stats.percent >= 50) {
      icon = '🟨';
    }

    dailyLines +=
      `${icon} ${dayNames[i]} — ` +
      `${stats.completed}/${stats.total} — ` +
      `${stats.percent}%\n`;
  }

  let comparison;

  if (previous.total === 0) {
    comparison =
      `ℹ️ Oldingi hafta uchun ma'lumot yo'q`;
  } else {
    const difference =
      current.percent -
      previous.percent;

    if (difference > 0) {
      comparison =
        `⬆️ +${difference} foiz punkt`;
    } else if (difference < 0) {
      comparison =
        `⬇️ ${Math.abs(difference)} foiz punkt`;
    } else {
      comparison =
        `➡️ 0 foiz punkt`;
    }
  }

  let conclusion;

  if (current.total === 0) {
    conclusion =
      `📭 Bu hafta uchun vazifalar topilmadi.`;
  } else if (current.percent >= 80) {
    conclusion =
      `🏆 *Ajoyib hafta!*\n` +
      `Shu tempni keyingi haftada ham saqlab qoling! 🔥`;
  } else if (current.percent >= 60) {
    conclusion =
      `👍 *Yaxshi hafta!*\n` +
      `Keyingi hafta natijani yanada oshiramiz. 💪`;
  } else if (current.percent >= 40) {
    conclusion =
      `📈 *O'sish uchun imkoniyat bor.*\n` +
      `Keyingi hafta yanada tartibliroq harakat qilamiz.`;
  } else {
    conclusion =
      `💪 *Taslim bo'lmang!*\n` +
      `Keyingi hafta yangi imkoniyat.`;
  }

  let text =
    `📊 *HAFTALIK HISOBOT*\n\n` +
    `📅 ${weekStart} — ${weekEnd}\n\n` +
    `🎯 Natija: *${current.percent}%*\n` +
    `${progressBar(current.percent)}\n\n` +
    `📋 Jami: ${current.total}\n` +
    `✅ Bajarildi: ${current.completed}\n` +
    `❌ Bajarilmadi: ${current.failed}\n`;

  if (current.pending > 0) {
    text +=
      `⏳ Belgilanmagan: ${current.pending}\n`;
  }

  text +=
    `\n━━━━━━━━━━━━━━\n\n` +
    `📈 Oldingi haftaga nisbatan:\n` +
    `${comparison}\n\n` +
    `━━━━━━━━━━━━━━\n\n` +
    `📅 *Kunlar bo'yicha:*\n\n` +
    dailyLines +
    `\n━━━━━━━━━━━━━━\n\n` +
    `💡 *HAFTA XULOSASI*\n\n` +
    conclusion;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

/* =========================================================
   /OYLIK
========================================================= */

async function handleMonthlyReport(chatId) {
  const user = await requireUser(chatId);

  if (!user) return;

  const today = getToday();

  const { year, month } =
    getDateParts(today);

  const monthNames = [
    'Yanvar',
    'Fevral',
    'Mart',
    'Aprel',
    'May',
    'Iyun',
    'Iyul',
    'Avgust',
    'Sentabr',
    'Oktabr',
    'Noyabr',
    'Dekabr'
  ];

  const monthStart =
    `${year}-${String(month).padStart(2, '0')}-01`;

  const nextMonth =
    month === 12 ? 1 : month + 1;

  const nextYear =
    month === 12 ? year + 1 : year;

  const monthEndDate =
    new Date(
      Date.UTC(
        nextYear,
        nextMonth - 1,
        0,
        12
      )
    );

  const monthEnd =
    monthEndDate
      .toISOString()
      .slice(0, 10);

  const previousMonth =
    month === 1 ? 12 : month - 1;

  const previousYear =
    month === 1 ? year - 1 : year;

  const previousMonthStart =
    `${previousYear}-${String(previousMonth).padStart(2, '0')}-01`;

  const previousMonthEndDate =
    new Date(
      Date.UTC(
        year,
        month - 1,
        0,
        12
      )
    );

  const previousMonthEnd =
    previousMonthEndDate
      .toISOString()
      .slice(0, 10);

  const result = await pool.query(
    `
    SELECT *
    FROM tasks
    WHERE user_id = $1
      AND task_date >= $2
      AND task_date <= $3
    `,
    [
      user.id,
      previousMonthStart,
      monthEnd
    ]
  );

  const allTasks = result.rows;

  const monthTasks =
    allTasks.filter(
      task =>
        task.task_date >= monthStart &&
        task.task_date <= monthEnd
    );

  const previousTasks =
    allTasks.filter(
      task =>
        task.task_date >= previousMonthStart &&
        task.task_date <= previousMonthEnd
    );

  const current = getStats(monthTasks);
  const previous = getStats(previousTasks);

  let comparison;

  if (previous.total === 0) {
    comparison =
      `➡️ O'tgan oy uchun ma'lumot yo'q`;
  } else {
    const difference =
      current.percent -
      previous.percent;

    if (difference > 0) {
      comparison =
        `⬆️ Natija: +${difference} foiz punkt`;
    } else if (difference < 0) {
      comparison =
        `⬇️ Natija: ${Math.abs(difference)} foiz punkt`;
    } else {
      comparison =
        `➡️ Natija: 0 foiz punkt`;
    }
  }

  let text =
    `📊 *OYLIK HISOBOT*\n\n` +
    `📅 ${monthNames[month - 1]} ${year}\n\n` +
    `🎯 Natija: *${current.percent}%*\n` +
    `${progressBar(current.percent)}\n\n` +
    `📋 Jami: ${current.total}\n` +
    `✅ Bajarildi: ${current.completed}\n` +
    `❌ Bajarilmadi: ${current.failed}`;

  if (current.pending > 0) {
    text +=
      `\n⏳ Belgilanmagan: ${current.pending}`;
  }

  text +=
    `\n\n━━━━━━━━━━━━━━\n\n` +
    `📈 ${monthNames[previousMonth - 1]} oyiga nisbatan:\n` +
    comparison;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

/* =========================================================
   /YILLIK
========================================================= */

async function handleYearlyReport(chatId) {
  const user = await requireUser(chatId);

  if (!user) return;

  const today = getToday();

  const { year } =
    getDateParts(today);

  const yearStart =
    `${year}-01-01`;

  const yearEnd =
    `${year}-12-31`;

  const previousYearStart =
    `${year - 1}-01-01`;

  const previousYearEnd =
    `${year - 1}-12-31`;

  const result = await pool.query(
    `
    SELECT *
    FROM tasks
    WHERE user_id = $1
      AND task_date >= $2
      AND task_date <= $3
    `,
    [
      user.id,
      previousYearStart,
      yearEnd
    ]
  );

  const allTasks = result.rows;

  const currentTasks =
    allTasks.filter(
      task =>
        task.task_date >= yearStart &&
        task.task_date <= yearEnd
    );

  const previousTasks =
    allTasks.filter(
      task =>
        task.task_date >= previousYearStart &&
        task.task_date <= previousYearEnd
    );

  const current =
    getStats(currentTasks);

  const previous =
    getStats(previousTasks);

  let comparison;

  if (previous.total === 0) {
    comparison =
      `➡️ O'tgan yil uchun ma'lumot yo'q`;
  } else {
    const difference =
      current.percent -
      previous.percent;

    if (difference > 0) {
      comparison =
        `⬆️ Natija: +${difference} foiz punkt`;
    } else if (difference < 0) {
      comparison =
        `⬇️ Natija: ${Math.abs(difference)} foiz punkt`;
    } else {
      comparison =
        `➡️ Natija: 0 foiz punkt`;
    }
  }

  const monthNames = [
    'Yanvar',
    'Fevral',
    'Mart',
    'Aprel',
    'May',
    'Iyun',
    'Iyul',
    'Avgust',
    'Sentabr',
    'Oktabr',
    'Noyabr',
    'Dekabr'
  ];

  const byMonth = {};

  for (const task of currentTasks) {
    const taskMonth =
      Number(
        String(task.task_date)
          .split('-')[1]
      );

    if (!byMonth[taskMonth]) {
      byMonth[taskMonth] = [];
    }

    byMonth[taskMonth].push(task);
  }

  let monthLines = '';

  for (let i = 1; i <= 12; i++) {
    const tasks = byMonth[i];

    if (!tasks || tasks.length === 0) {
      monthLines +=
        `⬜ ${monthNames[i - 1]} — Ma'lumot yo'q\n`;

      continue;
    }

    const stats = getStats(tasks);

    monthLines +=
      `📌 ${monthNames[i - 1]} — ` +
      `${stats.completed}/${stats.total} — ` +
      `${stats.percent}%\n`;
  }

  const text =
    `📊 *YILLIK HISOBOT*\n\n` +
    `📅 ${year}\n\n` +
    `🎯 Natija: *${current.percent}%*\n` +
    `${progressBar(current.percent)}\n\n` +
    `📋 Jami: ${current.total}\n` +
    `✅ Bajarildi: ${current.completed}\n` +
    `❌ Bajarilmadi: ${current.failed}\n` +
    `⏳ Belgilanmagan: ${current.pending}\n\n` +
    `━━━━━━━━━━━━━━\n\n` +
    `📈 O'tgan yilga nisbatan:\n` +
    `${comparison}\n\n` +
    `━━━━━━━━━━━━━━\n\n` +
    `📅 *Oylar bo'yicha:*\n\n` +
    monthLines;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

/* =========================================================
   /ADMIN
========================================================= */

async function handleAdmin(chatId) {
  const adminChatId =
    String(process.env.ADMIN_CHAT_ID || '');

  if (
    !adminChatId ||
    String(chatId) !== adminChatId
  ) {
    await bot.sendMessage(
      chatId,
      `⛔ Bu buyruq faqat admin uchun.`
    );

    return;
  }

  const usersResult =
    await pool.query(
      `
      SELECT *
      FROM users
      `
    );

  const users =
    usersResult.rows;

  const total =
    users.length;

  const active =
    users.filter(
      user => user.state === 'active'
    ).length;

  const inactive =
    users.filter(
      user =>
        user.state !== 'active' &&
        user.state !== 'completed'
    ).length;

  const completedToday =
    users.filter(
      user => user.state === 'completed'
    ).length;

  const trial =
    users.filter(
      user =>
        user.subscription_status === 'trial'
    ).length;

  const paid =
    users.filter(
      user =>
        user.subscription_status === 'paid'
    ).length;

  const expired =
    users.filter(
      user =>
        user.subscription_status === 'expired'
    ).length;

  const today = getToday();

  const todayUsers =
    users.filter(
      user =>
        user.created_at &&
        new Intl.DateTimeFormat(
          'en-CA',
          {
            timeZone: TIMEZONE
          }
        ).format(
          new Date(user.created_at)
        ) === today
    ).length;

  const sevenDaysAgo =
    new Date();

  sevenDaysAgo.setDate(
    sevenDaysAgo.getDate() - 7
  );

  const newLast7Days =
    users.filter(
      user =>
        user.created_at &&
        new Date(user.created_at) >=
          sevenDaysAgo
    ).length;

  const timeDistribution = {};

  for (const user of users) {
    if (!user.morning_time) continue;

    const time =
      String(user.morning_time)
        .slice(0, 5);

    timeDistribution[time] =
      (timeDistribution[time] || 0) + 1;
  }

  const topTimes =
    Object.entries(timeDistribution)
      .sort(
        (a, b) =>
          b[1] - a[1]
      )
      .slice(0, 3)
      .map(
        ([time, count]) =>
          `${time} — ${count} ta`
      )
      .join('\n');

  const activePercent =
    total > 0
      ? Math.round(
          active / total * 100
        )
      : 0;

  const noUsername =
    users.filter(
      user =>
        !user.telegram_username
    ).length;

  let text =
    `👤 *Admin panel — Foydalanuvchilar*\n\n`;

  text +=
    `📊 *Jami: ${total} ta*\n\n`;

  text +=
    `🟢 Faol: ${active} ta (${activePercent}%)\n`;

  text +=
    `✅ Bugun yakunlagan: ${completedToday} ta\n`;

  text +=
    `🔴 Nofaol: ${inactive} ta\n\n`;

  text +=
    `━━━━━━━━━━━━━━\n\n`;

  text +=
    `💳 *Obuna holati:*\n`;

  text +=
    `🆓 Trial: ${trial} ta\n`;

  text +=
    `💰 To'langan: ${paid} ta\n`;

  text +=
    `⌛ Muddati tugagan: ${expired} ta\n\n`;

  text +=
    `━━━━━━━━━━━━━━\n\n`;

  text +=
    `🆕 Bugun qo'shilgan: ${todayUsers} ta\n`;

  text +=
    `🆕 Oxirgi 7 kunda: ${newLast7Days} ta\n\n`;

  text +=
    `━━━━━━━━━━━━━━\n\n`;

  text +=
    `⏰ *Eng mashhur ertalabki vaqtlar:*\n`;

  text +=
    topTimes ||
    `Ma'lumot yo'q`;

  text +=
    `\n\n━━━━━━━━━━━━━━\n\n`;

  text +=
    `⚠️ Username yo'qlar: ${noUsername} ta`;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

/* =========================================================
   /XABAR
   Misollar:

   /xabar Salom hammaga!

   /xabar @ism:Shakhbozbek | Salom!

   /xabar @limit:20 | Salom

========================================================= */

async function handleBroadcast(
  chatId,
  messageText
) {
  const adminChatId =
    String(process.env.ADMIN_CHAT_ID || '');

  if (
    !adminChatId ||
    String(chatId) !== adminChatId
  ) {
    await bot.sendMessage(
      chatId,
      `⛔ Bu buyruq faqat admin uchun.`
    );

    return;
  }

  let raw =
    messageText
      .replace(/^\/xabar\s*/i, '')
      .trim();

  let filterName = null;
  let limit = null;

  const metaMatch =
    raw.match(
      /^((?:@\w+:[^\s|]+\s*)+)\|\s*([\s\S]*)$/
    );

  let broadcastText = raw;

  if (metaMatch) {
    const metaPart =
      metaMatch[1];

    broadcastText =
      metaMatch[2].trim();

    const nameMatch =
      metaPart.match(
        /@ism:([^\s]+)/i
      );

    if (nameMatch) {
      filterName =
        nameMatch[1]
          .trim()
          .toLowerCase();
    }

    const limitMatch =
      metaPart.match(
        /@limit:(\d+)/i
      );

    if (limitMatch) {
      limit =
        parseInt(
          limitMatch[1],
          10
        );
    }
  }

  if (!broadcastText) {
    await bot.sendMessage(
      chatId,
      `⚠️ Matn kiriting.

Misollar:

/xabar Salom hammaga!

/xabar @ism:Shakhbozbek | Salom!

/xabar @limit:20 | Salom birinchi 20 taga`
    );

    return;
  }

  const result =
    await pool.query(
      `
      SELECT
        telegram_chat_id,
        first_name,
        telegram_username
      FROM users
      `
    );

  let users =
    result.rows;

  if (filterName) {
    users =
      users.filter(user => {
        const firstName =
          String(
            user.first_name || ''
          ).toLowerCase();

        const username =
          String(
            user.telegram_username || ''
          ).toLowerCase();

        return (
          firstName === filterName ||
          username === filterName
        );
      });
  }

  if (limit !== null) {
    users =
      users.slice(0, limit);
  }

  if (users.length === 0) {
    await bot.sendMessage(
      chatId,
      `📭 Xabar yuborish uchun foydalanuvchi topilmadi.`
    );

    return;
  }

  let success = 0;
  let failed = 0;

  for (const user of users) {
    try {
      await bot.sendMessage(
        user.telegram_chat_id,
        broadcastText
      );

      success++;

    } catch (error) {
      failed++;

      console.error(
        'Broadcast error:',
        user.telegram_chat_id,
        error.message
      );
    }
  }

  await bot.sendMessage(
    chatId,
    `📨 *Xabar yuborish yakunlandi!*

👥 Tanlangan: ${users.length}
✅ Yuborildi: ${success}
❌ Xato: ${failed}`,
    {
      parse_mode: 'Markdown'
    }
  );
}

/* =========================================================
   ASOSIY WEBHOOK
========================================================= */

router.post(
  '/',
  async (req, res) => {
    const startTime =
      Date.now();

    try {
      const update =
        req.body;

      /*
        HTTP javobni tez qaytaramiz.
      */

      res.json({
        ok: true
      });

      const isCallback =
        Boolean(
          update.callback_query
        );

      const callbackData =
        update.callback_query?.data ||
        null;

      const messageText =
        (
          update.message?.text ||
          ''
        ).trim();

      const chatId =
        update.message?.chat?.id ||
        update.callback_query?.message?.chat?.id;

      const username =
        update.message?.chat?.username ||
        update.callback_query?.from?.username ||
        '';

      const firstName =
        update.message?.chat?.first_name ||
        update.callback_query?.from?.first_name ||
        "Do'st";

      const telegramMessage =
        update.callback_query?.message ||
        null;

      const callbackQueryId =
        update.callback_query?.id ||
        null;

      if (!chatId) {
        console.error(
          'chatId topilmadi'
        );

        return;
      }

      let route =
        'other';

      /*
        CALLBACK
      */

      if (
        isCallback &&
        callbackData
      ) {
        if (
          callbackData.startsWith(
            'morning_time|'
          )
        ) {
          route =
            'morning_callback';

        } else if (
          callbackData.startsWith(
            'task_status|'
          )
        ) {
          route =
            'task_status';
        }

      /*
        COMMANDLAR
      */

      } else if (
        messageText === '/start'
      ) {
        route = 'start';

      } else if (
        messageText === '/yakunladim'
      ) {
        route = 'yakunladim';

      } else if (
        messageText === '/hisobot'
      ) {
        route = 'hisobot';

      } else if (
        messageText === '/haftalik'
      ) {
        route = 'haftalik';

      } else if (
        messageText === '/oylik'
      ) {
        route = 'oylik';

      } else if (
        messageText === '/yillik'
      ) {
        route = 'yillik';

      } else if (
        messageText === '/admin'
      ) {
        route = 'admin';

      } else if (
        messageText.startsWith(
          '/xabar'
        )
      ) {
        route = 'broadcast';

      } else if (
        messageText &&
        !messageText.startsWith('/')
      ) {
        route =
          'task_text';
      }

      /*
        ROUTING
      */

      switch (route) {
        case 'start':
          await handleStart(
            chatId,
            firstName,
            username
          );
          break;

        case 'morning_callback':
          await handleMorningTime(
            chatId,
            callbackData,
            callbackQueryId
          );
          break;

        case 'task_text':
          await handleTaskWrite(
            chatId,
            messageText
          );
          break;

        case 'task_status':
          await handleTaskStatus(
            chatId,
            callbackData,
            callbackQueryId,
            telegramMessage
          );
          break;

        case 'yakunladim':
          await handleFinishDay(
            chatId
          );
          break;

        case 'hisobot':
          await handleDailyReport(
            chatId
          );
          break;

        case 'haftalik':
          await handleWeeklyReport(
            chatId
          );
          break;

        case 'oylik':
          await handleMonthlyReport(
            chatId
          );
          break;

        case 'yillik':
          await handleYearlyReport(
            chatId
          );
          break;

        case 'admin':
          await handleAdmin(
            chatId
          );
          break;

        case 'broadcast':
          await handleBroadcast(
            chatId,
            messageText
          );
          break;

        default:
          if (
            messageText.startsWith('/')
          ) {
            await bot.sendMessage(
              chatId,
              `⚠️ Noma'lum buyruq.

Mavjud buyruqlar:

/start
/yakunladim
/hisobot
/haftalik
/oylik
/yillik`
            );
          }
      }

      const duration =
        Date.now() -
        startTime;

      console.log(
        `[${route}] ${chatId} - ${duration}ms`
      );

    } catch (error) {
      console.error(
        'TELEGRAM ERROR:',
        error
      );

      /*
        Javob hali yuborilmagan bo'lsa
      */

      if (!res.headersSent) {
        res.status(500).json({
          error:
            error.message
        });
      }
    }
  }
);

module.exports = router;
