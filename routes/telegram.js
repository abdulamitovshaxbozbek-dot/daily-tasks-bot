const express = require('express');
const router = express.Router();

const pool = require('../db');
const TelegramAPI = require('node-telegram-bot-api');

const bot = new TelegramAPI(process.env.TELEGRAM_TOKEN);

// =========================
// CONFIG
// =========================

const ADMIN_CHAT_ID = 8908985083;
const TIMEZONE = 'Asia/Tashkent';

const MORNING_TIMES = [
  '02:00',
  '03:00',
  '04:00',
  '05:00',
  '06:00',
  '07:00',
  '08:00',
  '09:00',
  '10:00'
];

// =========================
// HELPERS
// =========================

function getTashkentDate(offsetDays = 0) {
  const now = new Date();

  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).formatToParts(now);

  const year = parts.find(p => p.type === 'year').value;
  const month = parts.find(p => p.type === 'month').value;
  const day = parts.find(p => p.type === 'day').value;

  const date = new Date(
    Date.UTC(
      Number(year),
      Number(month) - 1,
      Number(day) + offsetDays
    )
  );

  return date.toISOString().slice(0, 10);
}

function normalizeTask(text) {
  return text
    .toLowerCase()
    .replace(/^\s*(\d+[\.\)]|[-•*])\s*/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function cleanTask(text) {
  return text
    .replace(/^\s*(\d+[\.\)]|[-•*])\s*/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function progressBar(percent, length = 10) {
  const filled = Math.round((percent / 100) * length);

  return '🟩'.repeat(filled) + '⬜'.repeat(length - filled);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function isAdmin(chatId) {
  return Number(chatId) === ADMIN_CHAT_ID;
}

// =========================
// STATE MANAGEMENT
// =========================

async function changeUserState(userId, newState, reason = null) {
  const client = await pool.connect();

  try {
    await client.query('BEGIN');

    const current = await client.query(
      `SELECT state
       FROM users
       WHERE id = $1
       FOR UPDATE`,
      [userId]
    );

    if (current.rows.length === 0) {
      throw new Error('User topilmadi');
    }

    const oldState = current.rows[0].state;

    await client.query(
      `UPDATE users
       SET state = $1
       WHERE id = $2`,
      [newState, userId]
    );

    if (oldState !== newState) {
      await client.query(
        `INSERT INTO user_state_history
         (user_id, old_state, new_state, reason)
         VALUES ($1, $2, $3, $4)`,
        [userId, oldState, newState, reason]
      );
    }

    await client.query('COMMIT');

    return {
      oldState,
      newState
    };

  } catch (error) {
    await client.query('ROLLBACK');
    throw error;

  } finally {
    client.release();
  }
}

// =========================
// ROUTER
// =========================

router.post('/', async (req, res) => {
  const startTime = Date.now();

  try {
    const trigger = req.body;

    const chatId =
      trigger.message?.chat?.id ||
      trigger.callback_query?.message?.chat?.id;

    const messageText =
      (trigger.message?.text || '').trim();

    const callbackData =
      trigger.callback_query?.data || null;

    const callbackQueryId =
      trigger.callback_query?.id || null;

    const callbackMessage =
      trigger.callback_query?.message || null;

    const username =
      trigger.message?.chat?.username ||
      trigger.callback_query?.from?.username ||
      '';

    const firstName =
      trigger.message?.chat?.first_name ||
      trigger.callback_query?.from?.first_name ||
      "Do'st";

    if (!chatId) {
      return res.status(400).json({
        ok: false,
        error: 'chatId topilmadi'
      });
    }

    let route = 'extra';

    // =========================
    // ROUTE DETECTION
    // =========================

    if (messageText === '/start') {
      route = 'start';

    } else if (callbackData?.startsWith('morning_time|')) {
      route = 'morning_callback';

    } else if (callbackData?.startsWith('task_status|')) {
      route = 'task_status';

    } else if (messageText === '/yakunladim') {
      route = 'yakunladim';

    } else if (messageText === '/hisobot') {
      route = 'hisobot';

    } else if (messageText === '/haftalik') {
      route = 'haftalik';

    } else if (messageText === '/oylik') {
      route = 'oylik';

    } else if (messageText === '/yillik') {
      route = 'yillik';

    } else if (messageText === '/admin') {
      route = 'admin';

    } else if (messageText.startsWith('/xabar')) {
      route = 'broadcast';

    } else if (
      messageText &&
      !messageText.startsWith('/')
    ) {
      route = 'task_text';
    }

    // =========================
    // ROUTE EXECUTION
    // =========================

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

      case 'yakunladim':
        await handleYakunladim(chatId);
        break;

      case 'task_status':
        await handleTaskStatus(
          chatId,
          callbackData,
          callbackQueryId,
          callbackMessage
        );
        break;

      case 'hisobot':
        await handleDailyReport(chatId);
        break;

      case 'haftalik':
        await handleWeeklyReport(chatId);
        break;

      case 'oylik':
        await handleMonthlyReport(chatId);
        break;

      case 'yillik':
        await handleYearlyReport(chatId);
        break;

      case 'admin':
        await handleAdmin(chatId);
        break;

      case 'broadcast':
        await handleBroadcast(
          chatId,
          messageText
        );
        break;

      default:
        await handleExtra(chatId);
    }

    const duration = Date.now() - startTime;

    console.log(
      `[${route}] chat:${chatId} ${duration}ms`
    );

    res.json({
      ok: true,
      route,
      duration
    });

  } catch (error) {

    console.error('TELEGRAM ERROR:', error);

    res.status(500).json({
      ok: false,
      error: error.message
    });
  }
});

// =========================
// /START
// =========================

async function handleStart(
  chatId,
  firstName,
  username
) {

  const result = await pool.query(
    `SELECT *
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  // USER ALREADY EXISTS
  if (result.rows.length > 0) {

    await bot.sendMessage(
      chatId,
      `👋 Assalomu alaykum, ${firstName}!

Siz allaqachon ro'yxatdan o'tgansiz. ✅

📋 Vazifalaringizni yuborishni davom ettirishingiz mumkin.`
    );

    return;
  }

  // CREATE USER

  const newUser = await pool.query(
    `INSERT INTO users (
      telegram_chat_id,
      telegram_username,
      first_name,
      timezone,
      state,
      subscription_status
    )
    VALUES ($1, $2, $3, $4, $5, $6)
    RETURNING id`,
    [
      chatId,
      username,
      firstName,
      TIMEZONE,
      'waiting_morning_time',
      'trial'
    ]
  );

  const userId = newUser.rows[0].id;

  await pool.query(
    `INSERT INTO user_state_history
    (
      user_id,
      old_state,
      new_state,
      reason
    )
    VALUES ($1, $2, $3, $4)`,
    [
      userId,
      null,
      'waiting_morning_time',
      'user_started'
    ]
  );

  // BUTTONS 02:00 - 10:00

  const buttons = [];

  for (let i = 0; i < MORNING_TIMES.length; i += 3) {

    buttons.push(
      MORNING_TIMES
        .slice(i, i + 3)
        .map(time => ({
          text: time,
          callback_data: `morning_time|${time}`
        }))
    );
  }

  await bot.sendMessage(
    chatId,
    `👋 Assalomu alaykum, ${firstName}!

🌅 Ertalab qaysi vaqtda vazifalaringizni so'raylik?`,
    {
      reply_markup: {
        inline_keyboard: buttons
      }
    }
  );
}

// =========================
// MORNING TIME
// =========================

async function handleMorningTime(
  chatId,
  callbackData,
  callbackQueryId
) {

  const [, time] = callbackData.split('|');

  if (!MORNING_TIMES.includes(time)) {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Noto‘g‘ri vaqt tanlandi'
      }
    );

    return;
  }

  const userResult = await pool.query(
    `SELECT *
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Avval /start bering'
      }
    );

    return;
  }

  const user = userResult.rows[0];

  if (
    user.state !== 'waiting_morning_time'
  ) {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Vaqt allaqachon tanlangan'
      }
    );

    return;
  }

  await pool.query(
    `UPDATE users
     SET morning_time = $1
     WHERE id = $2`,
    [
      time,
      user.id
    ]
  );

  await changeUserState(
    user.id,
    'active',
    'morning_time_selected'
  );

  await bot.answerCallbackQuery(
    callbackQueryId,
    {
      text: `Ertalabki vaqt: ${time}`
    }
  );

  await bot.sendMessage(
    chatId,
    `✅ Ertalabki vaqt belgilandi: ${time}

🚀 Endi vazifalaringizni yuborishingiz mumkin!

Masalan:

Kitob o'qish
Sport qilish
Ingliz tili

🤲 Kuningiz barakatli o'tsin!`
  );
}

// =========================
// ADD TASKS
// =========================

async function handleTaskWrite(
  chatId,
  text
) {

  const userResult = await pool.query(
    `SELECT *
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      `⚠️ Avval /start buyrug'ini bering.`
    );

    return;
  }

  const user = userResult.rows[0];

  if (user.state !== 'active') {

    if (
      user.state ===
      'waiting_morning_time'
    ) {

      await bot.sendMessage(
        chatId,
        `⚠️ Avval ertalabki vaqtni tanlang.`
      );

    } else {

      await bot.sendMessage(
        chatId,
        `⚠️ Bugungi kun yakunlangan.

Yangi vazifalarni ertaga yuborishingiz mumkin.`
      );
    }

    return;
  }

  const today = getTashkentDate();

  const lines = text
    .split('\n')
    .map(cleanTask)
    .filter(Boolean);

  if (lines.length === 0) {

    await bot.sendMessage(
      chatId,
      `⚠️ Vazifa topilmadi.`
    );

    return;
  }

  const existingResult = await pool.query(
    `SELECT task_text
     FROM tasks
     WHERE user_id = $1
     AND task_date = $2`,
    [
      user.id,
      today
    ]
  );

  const existingNormalized = new Set(
    existingResult.rows.map(
      row => normalizeTask(row.task_text)
    )
  );

  let added = 0;
  let duplicates = 0;

  const currentMessageTasks = new Set();

  for (const originalText of lines) {

    const normalized =
      normalizeTask(originalText);

    if (
      !normalized ||
      existingNormalized.has(normalized) ||
      currentMessageTasks.has(normalized)
    ) {

      duplicates++;
      continue;
    }

    await pool.query(
      `INSERT INTO tasks (
        user_id,
        task_date,
        task_text,
        status
      )
      VALUES ($1, $2, $3, 'pending')`,
      [
        user.id,
        today,
        originalText
      ]
    );

    existingNormalized.add(normalized);
    currentMessageTasks.add(normalized);

    added++;
  }

  let message = '';

  if (added > 0) {

    message +=
      `🎉 ${added} ta yangi vazifa qo'shildi!\n`;
  }

  if (duplicates > 0) {

    message +=
      `🔄 ${duplicates} ta duplicate vazifa o'tkazib yuborildi.\n`;
  }

  if (added === 0 && duplicates > 0) {

    message =
      `📋 Bu vazifalar bugun allaqachon mavjud.`;
  }

  message += `

🤲 Kuningiz barakatli o'tsin!

🏁 Ishlaringiz tugaganda /yakunladim buyrug'ini bering.`;

  await bot.sendMessage(
    chatId,
    message
  );
}

// =========================
// /YAKUNLADIM
// =========================

async function handleYakunladim(chatId) {

  const userResult = await pool.query(
    `SELECT *
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      `⚠️ Avval /start bering.`
    );

    return;
  }

  const user = userResult.rows[0];

  if (user.state !== 'active') {

    await bot.sendMessage(
      chatId,
      `⚠️ Hozir vazifalarni yakunlash mumkin emas.`
    );

    return;
  }

  const today = getTashkentDate();

  const tasksResult = await pool.query(
    `SELECT *
     FROM tasks
     WHERE user_id = $1
     AND task_date = $2
     AND status = 'pending'
     ORDER BY created_at ASC`,
    [
      user.id,
      today
    ]
  );

  const tasks =
    tasksResult.rows;

  if (tasks.length === 0) {

    await bot.sendMessage(
      chatId,
      `📭 Bugun belgilanmagan vazifalar yo'q.

📊 Hisobot uchun /hisobot bering.`
    );

    return;
  }

  await bot.sendMessage(
    chatId,
    `🏁 Bugungi vazifalarni baholaymiz.

Har bir vazifa uchun natijani tanlang:`
  );

  for (const task of tasks) {

    const sentMessage =
      await bot.sendMessage(
        chatId,
        `📌 ${task.task_text}`,
        {
          reply_markup: {
            inline_keyboard: [
              [
                {
                  text: '✅ Bajarildi',
                  callback_data:
                    `task_status|completed|${task.id}`
                },
                {
                  text: '❌ Bajarilmadi',
                  callback_data:
                    `task_status|failed|${task.id}`
                }
              ]
            ]
          }
        }
      );

    await pool.query(
      `UPDATE tasks
       SET telegram_message_id = $1
       WHERE id = $2`,
      [
        sentMessage.message_id,
        task.id
      ]
    );
  }
}

// =========================
// TASK STATUS
// =========================

async function handleTaskStatus(
  chatId,
  callbackData,
  callbackQueryId,
  callbackMessage
) {

  const parts =
    callbackData.split('|');

  const status =
    parts[1];

  const taskId =
    parts[2];

  if (
    !['completed', 'failed']
      .includes(status)
  ) {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Noto‘g‘ri status'
      }
    );

    return;
  }

  const updateResult = await pool.query(
    `UPDATE tasks
     SET status = $1
     WHERE id = $2
     RETURNING user_id`,
    [
      status,
      taskId
    ]
  );

  if (
    updateResult.rows.length === 0
  ) {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Vazifa topilmadi'
      }
    );

    return;
  }

  const userId =
    updateResult.rows[0].user_id;

  const emoji =
    status === 'completed'
      ? '✅'
      : '❌';

  await bot.answerCallbackQuery(
    callbackQueryId,
    {
      text:
        status === 'completed'
          ? 'Bajarildi!'
          : 'Bajarilmadi!'
    }
  );

  try {

    await bot.deleteMessage(
      chatId,
      callbackMessage.message_id
    );

  } catch (error) {

    console.log(
      'Message delete failed:',
      error.message
    );
  }

  const today = getTashkentDate();

  const pendingResult =
    await pool.query(
      `SELECT COUNT(*)::int AS count
       FROM tasks
       WHERE user_id = $1
       AND task_date = $2
       AND status = 'pending'`,
      [
        userId,
        today
      ]
    );

  const pending =
    pendingResult.rows[0].count;

  if (pending === 0) {

    const stateResult =
      await pool.query(
        `UPDATE users
         SET state = 'completed'
         WHERE id = $1
         AND state = 'active'
         RETURNING id`,
        [userId]
      );

    if (
      stateResult.rows.length > 0
    ) {

      await pool.query(
        `INSERT INTO user_state_history
        (
          user_id,
          old_state,
          new_state,
          reason
        )
        VALUES ($1, 'active', 'completed', 'all_tasks_marked')`,
        [userId]
      );

      await bot.sendMessage(
        chatId,
        `🎉 Barcha vazifalar belgilandi!

📊 Natijani ko'rish uchun /hisobot bering.`
      );
    }
  }
}

// =========================
// DAILY REPORT
// =========================

async function handleDailyReport(chatId) {

  const userResult =
    await pool.query(
      `SELECT *
       FROM users
       WHERE telegram_chat_id = $1`,
      [chatId]
    );

  if (
    userResult.rows.length === 0
  ) {

    await bot.sendMessage(
      chatId,
      `⚠️ User topilmadi.`
    );

    return;
  }

  const user =
    userResult.rows[0];

  const today =
    getTashkentDate();

  const yesterday =
    getTashkentDate(-1);

  const result =
    await pool.query(
      `SELECT
        task_date,
        status,
        COUNT(*)::int AS count
      FROM tasks
      WHERE user_id = $1
      AND task_date IN ($2, $3)
      GROUP BY task_date, status`,
      [
        user.id,
        today,
        yesterday
      ]
    );

  const stats = {
    today: {
      completed: 0,
      failed: 0,
      pending: 0
    },
    yesterday: {
      completed: 0,
      failed: 0,
      pending: 0
    }
  };

  for (
    const row of result.rows
  ) {

    if (
      row.task_date === today
    ) {

      stats.today[row.status] =
        row.count;

    } else {

      stats.yesterday[row.status] =
        row.count;
    }
  }

  const todayTotal =
    Object.values(
      stats.today
    ).reduce(
      (a, b) => a + b,
      0
    );

  const yesterdayTotal =
    Object.values(
      stats.yesterday
    ).reduce(
      (a, b) => a + b,
      0
    );

  const todayPercent =
    todayTotal > 0
      ? Math.round(
          stats.today.completed /
          todayTotal *
          100
        )
      : 0;

  const yesterdayPercent =
    yesterdayTotal > 0
      ? Math.round(
          stats.yesterday.completed /
          yesterdayTotal *
          100
        )
      : 0;

  const difference =
    todayPercent -
    yesterdayPercent;

  let comparison = '';

  if (
    yesterdayTotal === 0
  ) {

    comparison =
      `📅 Kechagi ma'lumot mavjud emas.`;

  } else if (
    difference > 0
  ) {

    comparison =
      `📈 Kechagidan ${difference}% yaxshiroq!`;

  } else if (
    difference < 0
  ) {

    comparison =
      `📉 Kechagiga nisbatan ${Math.abs(difference)}% past.`;

  } else {

    comparison =
      `➖ Kechagi natija bilan bir xil.`;
  }

  let motivation = '';

  if (
    todayPercent === 100 &&
    todayTotal > 0
  ) {

    motivation =
      `🔥 Zo'r! Bugungi barcha vazifalarni bajardingiz!`;

  } else if (
    todayPercent >= 70
  ) {

    motivation =
      `💪 Juda yaxshi natija! Shu tempni davom ettiring.`;

  } else if (
    todayPercent >= 40
  ) {

    motivation =
      `🚀 Yaxshi. Ertaga yanada kuchliroq bo'lasiz!`;

  } else {

    motivation =
      `🌱 Har bir kichik qadam ham natija. Ertaga qayta urinib ko'ramiz!`;
  }

  const text =
`📊 *KUNLIK XULOSA*

${progressBar(todayPercent)}

🎯 *Natija: ${todayPercent}%*

✅ Bajarildi: ${stats.today.completed}
❌ Bajarilmadi: ${stats.today.failed}
⏳ Belgilanmagan: ${stats.today.pending}

📋 Jami: ${todayTotal}

${comparison}

${motivation}`;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );

  if (
    user.state === 'active'
  ) {

    await changeUserState(
      user.id,
      'completed',
      'daily_report'
    );
  }
}

// =========================
// WEEKLY REPORT
// =========================

async function handleWeeklyReport(
  chatId
) {

  const user =
    await getUser(chatId);

  if (!user) return;

  const today =
    getTashkentDate();

  const start =
    getTashkentDate(-6);

  const result =
    await pool.query(
      `SELECT
        task_date,
        status,
        COUNT(*)::int AS count
      FROM tasks
      WHERE user_id = $1
      AND task_date BETWEEN $2 AND $3
      GROUP BY task_date, status
      ORDER BY task_date`,
      [
        user.id,
        start,
        today
      ]
    );

  const days = {};

  for (
    const row of result.rows
  ) {

    if (!days[row.task_date]) {

      days[row.task_date] = {
        completed: 0,
        failed: 0,
        pending: 0
      };
    }

    days[row.task_date][row.status] =
      row.count;
  }

  let total = 0;
  let completed = 0;

  let bestDay = null;
  let bestPercent = -1;

  let worstDay = null;
  let worstPercent = 101;

  const lines = [];

  for (
    const date of Object.keys(days)
  ) {

    const stat =
      days[date];

    const dayTotal =
      stat.completed +
      stat.failed +
      stat.pending;

    const percent =
      dayTotal > 0
        ? Math.round(
            stat.completed /
            dayTotal *
            100
          )
        : 0;

    total += dayTotal;
    completed += stat.completed;

    lines.push(
      `📅 ${date}: ${percent}% (${stat.completed}/${dayTotal})`
    );

    if (
      dayTotal > 0 &&
      percent > bestPercent
    ) {

      bestPercent = percent;
      bestDay = date;
    }

    if (
      dayTotal > 0 &&
      percent < worstPercent
    ) {

      worstPercent = percent;
      worstDay = date;
    }
  }

  const percent =
    total > 0
      ? Math.round(
          completed / total * 100
        )
      : 0;

  const text =
`📊 *HAFTALIK XULOSA*

${progressBar(percent)}

🎯 Umumiy natija: *${percent}%*

✅ Bajarildi: ${completed}/${total}

${bestDay ? `🏆 Eng yaxshi kun: ${bestDay} — ${bestPercent}%` : ''}
${worstDay ? `📉 Sust kun: ${worstDay} — ${worstPercent}%` : ''}

*Kunlar bo'yicha:*

${lines.length ? lines.join('\n') : 'Ma\'lumot mavjud emas.'}`;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

// =========================
// MONTHLY REPORT
// =========================

async function handleMonthlyReport(
  chatId
) {

  const user =
    await getUser(chatId);

  if (!user) return;

  const today =
    getTashkentDate();

  const [year, month] =
    today.split('-');

  const start =
    `${year}-${month}-01`;

  const result =
    await pool.query(
      `SELECT
        status,
        COUNT(*)::int AS count
      FROM tasks
      WHERE user_id = $1
      AND task_date >= $2
      AND task_date <= $3
      GROUP BY status`,
      [
        user.id,
        start,
        today
      ]
    );

  const stats = {
    completed: 0,
    failed: 0,
    pending: 0
  };

  result.rows.forEach(row => {
    stats[row.status] =
      row.count;
  });

  const total =
    stats.completed +
    stats.failed +
    stats.pending;

  const percent =
    total > 0
      ? Math.round(
          stats.completed /
          total *
          100
        )
      : 0;

  const text =
`📊 *OYLIK XULOSA*

${progressBar(percent)}

🎯 Natija: *${percent}%*

✅ Bajarildi: ${stats.completed}
❌ Bajarilmadi: ${stats.failed}
⏳ Belgilanmagan: ${stats.pending}

📋 Jami: ${total}`;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

// =========================
// YEARLY REPORT
// =========================

async function handleYearlyReport(
  chatId
) {

  const user =
    await getUser(chatId);

  if (!user) return;

  const today =
    getTashkentDate();

  const year =
    today.slice(0, 4);

  const start =
    `${year}-01-01`;

  const result =
    await pool.query(
      `SELECT
        TO_CHAR(
          task_date,
          'YYYY-MM'
        ) AS month,
        status,
        COUNT(*)::int AS count
      FROM tasks
      WHERE user_id = $1
      AND task_date >= $2
      AND task_date <= $3
      GROUP BY month, status
      ORDER BY month`,
      [
        user.id,
        start,
        today
      ]
    );

  const months = {};

  for (
    const row of result.rows
  ) {

    if (!months[row.month]) {

      months[row.month] = {
        completed: 0,
        failed: 0,
        pending: 0
      };
    }

    months[row.month][row.status] =
      row.count;
  }

  let total = 0;
  let completed = 0;

  const lines = [];

  let bestMonth = null;
  let bestPercent = -1;

  for (
    const month of Object.keys(months)
  ) {

    const stat =
      months[month];

    const monthTotal =
      stat.completed +
      stat.failed +
      stat.pending;

    const percent =
      monthTotal > 0
        ? Math.round(
            stat.completed /
            monthTotal *
            100
          )
        : 0;

    total += monthTotal;
    completed += stat.completed;

    lines.push(
      `📅 ${month}: ${percent}% (${stat.completed}/${monthTotal})`
    );

    if (
      percent > bestPercent
    ) {

      bestPercent = percent;
      bestMonth = month;
    }
  }

  const percent =
    total > 0
      ? Math.round(
          completed /
          total *
          100
        )
      : 0;

  const text =
`📊 *YILLIK XULOSA*

${progressBar(percent)}

🎯 Umumiy natija: *${percent}%*

✅ Bajarildi: ${completed}/${total}

${bestMonth ? `🏆 Eng yaxshi oy: ${bestMonth} — ${bestPercent}%` : ''}

*Oylar bo'yicha:*

${lines.length ? lines.join('\n') : 'Ma\'lumot mavjud emas.'}`;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

// =========================
// ADMIN
// =========================

async function handleAdmin(chatId) {

  if (!isAdmin(chatId)) {

    await bot.sendMessage(
      chatId,
      `⛔ Sizda admin huquqi yo'q.`
    );

    return;
  }

  const today =
    getTashkentDate();

  const weekAgo =
    getTashkentDate(-6);

  const stats =
    await pool.query(
      `
      SELECT
        COUNT(*)::int AS total_users,

        COUNT(*) FILTER (
          WHERE state = 'active'
        )::int AS active_users,

        COUNT(*) FILTER (
          WHERE state = 'completed'
        )::int AS completed_users,

        COUNT(*) FILTER (
          WHERE subscription_status = 'trial'
        )::int AS trial_users,

        COUNT(*) FILTER (
          WHERE subscription_status = 'paid'
        )::int AS paid_users,

        COUNT(*) FILTER (
          WHERE created_at >= $1::date
        )::int AS new_users
      FROM users
      `,
      [weekAgo]
    );

  const morningTimes =
    await pool.query(
      `
      SELECT
        morning_time,
        COUNT(*)::int AS count
      FROM users
      WHERE morning_time IS NOT NULL
      GROUP BY morning_time
      ORDER BY count DESC
      LIMIT 5
      `
    );

  const s =
    stats.rows[0];

  const timesText =
    morningTimes.rows.length
      ? morningTimes.rows
          .map(
            r =>
              `🕐 ${String(r.morning_time).slice(0, 5)} — ${r.count} ta`
          )
          .join('\n')
      : 'Ma\'lumot yo\'q';

  const text =
`👑 *ADMIN PANEL*

👥 Jami userlar: *${s.total_users}*
🟢 Faol: *${s.active_users}*
🏁 Yakunlagan: *${s.completed_users}*

🆓 Trial: *${s.trial_users}*
💳 Paid: *${s.paid_users}*

📅 Oxirgi 7 kunda yangi: *${s.new_users}*

🌅 *Mashhur ertalabki vaqtlar:*

${timesText}`;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

// =========================
// BROADCAST
// =========================

async function handleBroadcast(
  chatId,
  messageText
) {

  if (!isAdmin(chatId)) {

    await bot.sendMessage(
      chatId,
      `⛔ Sizda admin huquqi yo'q.`
    );

    return;
  }

  let content =
    messageText
      .replace(/^\/xabar\s*/i, '')
      .trim();

  if (!content) {

    await bot.sendMessage(
      chatId,
      `⚠️ Format:

/xabar Sizning xabaringiz

Qo'shimcha:

@ism:Ali
@limit:20`
    );

    return;
  }

  let nameFilter = null;
  let limit = null;

  const nameMatch =
    content.match(
      /@ism:([^\n]+)/i
    );

  if (nameMatch) {

    nameFilter =
      nameMatch[1].trim();

    content =
      content
        .replace(
          nameMatch[0],
          ''
        )
        .trim();
  }

  const limitMatch =
    content.match(
      /@limit:(\d+)/i
    );

  if (limitMatch) {

    limit =
      Number(limitMatch[1]);

    content =
      content
        .replace(
          limitMatch[0],
          ''
        )
        .trim();
  }

  let query =
    `SELECT id,
            telegram_chat_id,
            first_name
     FROM users`;

  const params = [];

  if (nameFilter) {

    params.push(
      `%${nameFilter}%`
    );

    query +=
      ` WHERE first_name ILIKE $${params.length}`;
  }

  query +=
    ` ORDER BY created_at ASC`;

  if (limit) {

    query +=
      ` LIMIT ${Math.max(
        1,
        Math.min(limit, 10000)
      )}`;
  }

  const usersResult =
    await pool.query(
      query,
      params
    );

  const users =
    usersResult.rows;

  if (
    users.length === 0
  ) {

    await bot.sendMessage(
      chatId,
      `📭 Mos user topilmadi.`
    );

    return;
  }

  const broadcastId =
    `broadcast_${Date.now()}`;

  let sent = 0;
  let failed = 0;

  await bot.sendMessage(
    chatId,
    `📢 Broadcast boshlandi.

👥 Userlar: ${users.length}`
  );

  for (
    let i = 0;
    i < users.length;
    i++
  ) {

    const user =
      users[i];

    try {

      await bot.sendMessage(
        user.telegram_chat_id,
        content
      );

      sent++;

      await pool.query(
        `INSERT INTO broadcast_logs
        (
          broadcast_id,
          user_id,
          status,
          sent_at
        )
        VALUES ($1, $2, $3, NOW())`,
        [
          broadcastId,
          user.id,
          'sent'
        ]
      );

    } catch (error) {

      failed++;

      await pool.query(
        `INSERT INTO broadcast_logs
        (
          broadcast_id,
          user_id,
          status,
          error_message
        )
        VALUES ($1, $2, $3, $4)`,
        [
          broadcastId,
          user.id,
          'failed',
          error.message
        ]
      );
    }

    // 20 tadan keyin batch pause

    if (
      (i + 1) % 20 === 0
    ) {

      await sleep(150);
    }
  }

  await bot.sendMessage(
    chatId,
    `📢 *Broadcast tugadi*

✅ Yuborildi: ${sent}
❌ Xato: ${failed}
👥 Jami: ${users.length}`,
    {
      parse_mode: 'Markdown'
    }
  );
}

// =========================
// EXTRA COMMAND
// =========================

async function handleExtra(chatId) {

  await bot.sendMessage(
    chatId,
    `❓ Buyruq topilmadi.

Mavjud buyruqlar:

/yakunladim — bugungi vazifalarni yakunlash
/hisobot — kunlik hisobot
/haftalik — haftalik hisobot
/oylik — oylik hisobot
/yillik — yillik hisobot`
  );
}

// =========================
// GET USER
// =========================

async function getUser(chatId) {

  const result =
    await pool.query(
      `SELECT *
       FROM users
       WHERE telegram_chat_id = $1`,
      [chatId]
    );

  if (
    result.rows.length === 0
  ) {

    await bot.sendMessage(
      chatId,
      `⚠️ Avval /start buyrug'ini bering.`
    );

    return null;
  }

  return result.rows[0];
}

module.exports = router;
