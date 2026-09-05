const express = require('express');
const router = express.Router();

const pool = require('../db');
const TelegramAPI = require('node-telegram-bot-api');

const bot = new TelegramAPI(process.env.TELEGRAM_TOKEN);

// ================================
// SOZLAMALAR
// ================================

const ADMIN_CHAT_ID = '8908985083';
const TIMEZONE = 'Asia/Tashkent';

// ================================
// YORDAMCHI FUNKSIYALAR
// ================================

// Tashkent bo'yicha YYYY-MM-DD
function getTashkentDate(date = new Date()) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }).format(date);
}

// Tashkent bo'yicha kechagi sana
function getYesterdayDate() {
  const now = new Date();

  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: TIMEZONE,
    year: 'numeric',
    month: 'numeric',
    day: 'numeric'
  }).formatToParts(now);

  const year = Number(parts.find(x => x.type === 'year').value);
  const month = Number(parts.find(x => x.type === 'month').value);
  const day = Number(parts.find(x => x.type === 'day').value);

  const tashkentDate = new Date(year, month - 1, day);
  tashkentDate.setDate(tashkentDate.getDate() - 1);

  return tashkentDate.toISOString().slice(0, 10);
}

// YYYY-MM-DD dan chiroyli sana
function formatUzDate(dateString) {
  const date = new Date(`${dateString}T12:00:00`);

  const months = [
    'yanvar',
    'fevral',
    'mart',
    'aprel',
    'may',
    'iyun',
    'iyul',
    'avgust',
    'sentabr',
    'oktabr',
    'noyabr',
    'dekabr'
  ];

  return `${date.getDate()}-${months[date.getMonth()]}, ${date.getFullYear()}`;
}

function normalizeTask(text) {
  return text
    .toLowerCase()
    .replace(/^\s*\d+[\.\)\-]\s*/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function cleanTaskText(text) {
  return text
    .replace(/^\s*\d+[\.\)\-]\s*/, '')
    .trim();
}

function progressBar(percent) {
  const filled = Math.round(percent / 10);
  const empty = 10 - filled;

  return '🟩'.repeat(filled) + '⬜'.repeat(empty);
}

function getMotivation(percent) {
  if (percent === 100) {
    return {
      title: '🔥 Ajoyib natija!',
      text: 'Barcha vazifalaringizni bajardingiz. Shu tempni davom ettiring! 🚀'
    };
  }

  if (percent >= 70) {
    return {
      title: '👍 Yaxshi natija!',
      text: "Natija yomon emas. Ertaga yana bir qadam oldinga! 💪"
    };
  }

  if (percent >= 40) {
    return {
      title: '💪 Harakat davom etsin!',
      text: "Bugun ham foydali ishlar qilindi. Ertaga bundan ham yaxshiroq bo'ladi!"
    };
  }

  if (percent > 0) {
    return {
      title: '🌱 Boshlanish bor!',
      text: "Muhimi — to'xtamaslik. Ertaga yangi imkoniyat!"
    };
  }

  return {
    title: '📭 Bugun natija yo‘q',
    text: "Ertaga yangidan boshlaymiz. Kichik qadamlar katta natijaga olib boradi! 💪"
  };
}

function calculateStats(tasks) {
  const stats = {
    total: tasks.length,
    completed: 0,
    failed: 0,
    pending: 0
  };

  for (const task of tasks) {
    if (task.status === 'completed') stats.completed++;
    else if (task.status === 'failed') stats.failed++;
    else stats.pending++;
  }

  stats.percent =
    stats.total > 0
      ? Math.round((stats.completed / stats.total) * 100)
      : 0;

  return stats;
}

function isAdmin(chatId) {
  return String(chatId) === ADMIN_CHAT_ID;
}

// ================================
// ASOSIY WEBHOOK
// ================================

router.post('/', async (req, res) => {
  const startTime = Date.now();

  try {
    const trigger = req.body || {};

    const message = trigger.message;
    const callbackQuery = trigger.callback_query;

    const chatId =
      message?.chat?.id ||
      callbackQuery?.message?.chat?.id;

    if (!chatId) {
      return res.json({
        ok: true,
        ignored: true,
        reason: 'No chat ID'
      });
    }

    const messageText = (message?.text || '').trim();
    const callbackData = callbackQuery?.data || '';

    const username =
      message?.chat?.username ||
      callbackQuery?.from?.username ||
      '';

    const firstName =
      message?.chat?.first_name ||
      callbackQuery?.from?.first_name ||
      "Do'st";

    let route = 'extra';

    if (messageText === '/start') {
      route = 'start';
    }

    else if (callbackData.startsWith('morning_time|')) {
      route = 'morning_callback';
    }

    else if (callbackData.startsWith('task_status|')) {
      route = 'task_status';
    }

    else if (messageText === '/yakunladim') {
      route = 'yakunladim';
    }

    else if (messageText === '/hisobot') {
      route = 'hisobot';
    }

    else if (messageText === '/haftalik') {
      route = 'haftalik';
    }

    else if (messageText === '/oylik') {
      route = 'oylik';
    }

    else if (messageText === '/yillik') {
      route = 'yillik';
    }

    else if (messageText === '/admin') {
      route = 'admin';
    }

    else if (messageText.startsWith('/xabar')) {
      route = 'broadcast';
    }

    else if (messageText && !messageText.startsWith('/')) {
      route = 'task_text';
    }

    switch (route) {

      case 'start':
        await handleStart(chatId, firstName, username);
        break;

      case 'morning_callback':
        await handleMorningTime(
          chatId,
          callbackData,
          callbackQuery.id
        );
        break;

      case 'task_text':
        await handleTaskWrite(chatId, messageText);
        break;

      case 'yakunladim':
        await handleFinishDay(chatId);
        break;

      case 'task_status':
        await handleTaskStatus(
          chatId,
          callbackData,
          callbackQuery.id,
          callbackQuery.message
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
        await handleBroadcast(chatId, messageText);
        break;

      default:
        if (messageText.startsWith('/')) {
          await bot.sendMessage(
            chatId,
            "⚠️ Bu buyruq mavjud emas."
          );
        }
        break;
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

    if (!res.headersSent) {
      res.status(500).json({
        ok: false,
        error: error.message
      });
    }
  }
});

// ================================
// /START
// ================================

async function handleStart(chatId, firstName, username) {

  const userResult = await pool.query(
    `SELECT *
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  // Eski user qayta ro'yxatdan o'tmaydi
  if (userResult.rows.length > 0) {

    await bot.sendMessage(
      chatId,
      `👋 Assalomu alaykum, ${firstName}!

Siz allaqachon ro'yxatdan o'tgansiz. ✅

📋 Vazifalaringizni yuborishni davom ettirishingiz mumkin.`
    );

    return;
  }

  // Yangi user
  await pool.query(
    `INSERT INTO users
    (
      telegram_chat_id,
      telegram_username,
      first_name,
      timezone,
      state,
      subscription_status
    )
    VALUES ($1, $2, $3, $4, $5, $6)`,
    [
      chatId,
      username,
      firstName,
      TIMEZONE,
      'waiting_morning_time',
      'trial'
    ]
  );

  const times = [
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

  const keyboard = [];

  for (let i = 0; i < times.length; i += 3) {

    keyboard.push(
      times.slice(i, i + 3).map(time => ({
        text: time,
        callback_data: `morning_time|${time}`
      }))
    );
  }

  await bot.sendMessage(
    chatId,
    `🌅 Assalomu alaykum, ${firstName}!

📋 Kunlik vazifalar botiga xush kelibsiz.

Tizim to'liq ishga tushishi uchun savolimga javob bering:

🕐 Kuningizni soat nechchida rejalashtirasiz? Vaqtni tanlang:`,
    {
      reply_markup: {
        inline_keyboard: keyboard
      }
    }
  );
}

// ================================
// ERTALABKI VAQT
// ================================

async function handleMorningTime(
  chatId,
  callbackData,
  callbackQueryId
) {

  const time = callbackData.split('|')[1];

  const userResult = await pool.query(
    `SELECT state
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Avval /start bering.',
        show_alert: true
      }
    );

    return;
  }

  const user = userResult.rows[0];

  if (user.state !== 'waiting_morning_time') {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Siz allaqachon vaqt tanlagansiz.',
        show_alert: false
      }
    );

    return;
  }

  await pool.query(
    `UPDATE users
     SET
       morning_time = $1,
       state = 'active'
     WHERE telegram_chat_id = $2`,
    [time, chatId]
  );

  await bot.answerCallbackQuery(callbackQueryId);

  await bot.sendMessage(
    chatId,
    `✅ **Ertalabki vaqt belgilandi:** ${time}

🚀 Hammasi tayyor. Endi kunlik vazifalaringizni yuborishingiz mumkin.

⏰ Belgilangan vaqtda sizga eslatma yuboramiz.

📢 Yangiliklar va yangilanishlar: @kunlikvazifalar_news

🤲 Kuningiz barakali o'tsin!`,
    {
      parse_mode: 'Markdown'
    }
  );
}

// ================================
// VAZIFA QO'SHISH
// ================================

async function handleTaskWrite(chatId, text) {

  const userResult = await pool.query(
    `SELECT id, state
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      "⚠️ Avval /start buyrug'ini bering."
    );

    return;
  }

  const user = userResult.rows[0];

  if (user.state !== 'active') {

    if (user.state === 'completed') {

      await bot.sendMessage(
        chatId,
        `🏁 Siz bugungi vazifalarni yakunlab bo'lgansiz.

📊 Natijani ko'rish uchun /hisobot buyrug'ini yuboring.`
      );

    } else {

      await bot.sendMessage(
        chatId,
        "⚠️ Avval /start buyrug'ini bering."
      );
    }

    return;
  }

  const today = getTashkentDate();

  const taskLines = text
    .split('\n')
    .map(cleanTaskText)
    .filter(Boolean);

  if (taskLines.length === 0) {

    await bot.sendMessage(
      chatId,
      "⚠️ Vazifa matni bo'sh."
    );

    return;
  }

  const existingResult = await pool.query(
    `SELECT task_text
     FROM tasks
     WHERE
       user_id = $1
       AND task_date = $2`,
    [user.id, today]
  );

  const existingTasks = new Set(
    existingResult.rows.map(row =>
      normalizeTask(row.task_text)
    )
  );

  let addedCount = 0;
  let duplicateCount = 0;

  const currentInputTasks = new Set();

  for (const rawTaskText of taskLines) {

    const normalized = normalizeTask(rawTaskText);

    if (
      existingTasks.has(normalized) ||
      currentInputTasks.has(normalized)
    ) {
      duplicateCount++;
      continue;
    }

    await pool.query(
      `INSERT INTO tasks
      (
        user_id,
        task_date,
        task_text,
        status
      )
      VALUES ($1, $2, $3, 'pending')`,
      [
        user.id,
        today,
        rawTaskText
      ]
    );

    existingTasks.add(normalized);
    currentInputTasks.add(normalized);

    addedCount++;
  }

  let response = '';

  if (addedCount > 0) {

    response +=
      `🎉 ${addedCount} ta yangi vazifa qabul qilindi va saqlandi!\n\n`;
  }

  if (duplicateCount > 0) {

    response +=
      `🔄 ${duplicateCount} ta vazifa bugun allaqachon qo'shilgan.\n\n♻️ Qayta saqlanmadi.\n\n`;
  }

  response += `🤲 Kuningiz barakatli o'tsin!`;

  if (addedCount > 0) {

    response +=
      `\n\n🏁 Kuningizni yakunlaganingizda /yakunladim buyrug'ini yuboring.`;
  }

  await bot.sendMessage(chatId, response);
}

// ================================
// /YAKUNLADIM
// A VARIANT:
// STATE SHU ZAHOTI COMPLETED
// ================================

async function handleFinishDay(chatId) {

  const userResult = await pool.query(
    `SELECT id, state
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      "⚠️ Avval /start buyrug'ini bering."
    );

    return;
  }

  const user = userResult.rows[0];

  if (user.state === 'completed') {

    await bot.sendMessage(
      chatId,
      `🏁 Siz bugungi kunni allaqachon yakunlagansiz.

📊 Natijani ko'rish uchun /hisobot buyrug'ini yuboring.`
    );

    return;
  }

  const today = getTashkentDate();

  const tasksResult = await pool.query(
    `SELECT *
     FROM tasks
     WHERE
       user_id = $1
       AND task_date = $2
       AND status = 'pending'
     ORDER BY created_at ASC`,
    [
      user.id,
      today
    ]
  );

  if (tasksResult.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      `📭 Bugun uchun belgilanmagan vazifalar topilmadi.`
    );

    return;
  }

  // A VARIANT
  // User /yakunladim bosishi bilan completed
  await pool.query(
    `UPDATE users
     SET state = 'completed'
     WHERE id = $1`,
    [user.id]
  );

  let number = 1;

  for (const task of tasksResult.rows) {

    await bot.sendMessage(
      chatId,
      `📋 ${number}. ${task.task_text}

Vazifa holatini belgilang:`,
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

    number++;
  }
}

// ================================
// TASK STATUS
// ================================

async function handleTaskStatus(
  chatId,
  callbackData,
  callbackQueryId,
  message
) {

  const parts = callbackData.split('|');

  const status = parts[1];
  const taskId = parts[2];

  if (
    status !== 'completed' &&
    status !== 'failed'
  ) {

    await bot.answerCallbackQuery(
      callbackQueryId,
      {
        text: 'Noto‘g‘ri status.'
      }
    );

    return;
  }

  const result = await pool.query(
    `UPDATE tasks
     SET status = $1
     WHERE id = $2
     RETURNING *`,
    [
      status,
      taskId
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

  const emoji =
    status === 'completed'
      ? '✅'
      : '❌';

  await bot.answerCallbackQuery(
    callbackQueryId,
    {
      text: `${emoji} Saqlandi!`
    }
  );

  try {

    await bot.deleteMessage(
      message.chat.id,
      message.message_id
    );

  } catch (error) {

    console.log(
      'Message delete failed:',
      error.message
    );
  }

  const today = getTashkentDate();

  const userResult = await pool.query(
    `SELECT id
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) return;

  const pendingResult = await pool.query(
    `SELECT COUNT(*)::int AS count
     FROM tasks
     WHERE
       user_id = $1
       AND task_date = $2
       AND status = 'pending'`,
    [
      userResult.rows[0].id,
      today
    ]
  );

  const pendingCount =
    pendingResult.rows[0].count;

  if (pendingCount === 0) {

    await bot.sendMessage(
      chatId,
      `🎉 Barcha vazifalar belgilandi!

📊 Endi /hisobot buyrug'ini bersangiz, bugungi hisobotingizni yuboraman.`
    );
  }
}

// ================================
// /HISOBOT
// BUGUN + KECHA
// ================================

async function handleDailyReport(chatId) {

  const userResult = await pool.query(
    `SELECT id
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      "⚠️ User topilmadi."
    );

    return;
  }

  const userId = userResult.rows[0].id;

  const today = getTashkentDate();
  const yesterday = getYesterdayDate();

  const todayResult = await pool.query(
    `SELECT *
     FROM tasks
     WHERE
       user_id = $1
       AND task_date = $2
     ORDER BY created_at ASC`,
    [
      userId,
      today
    ]
  );

  const yesterdayResult = await pool.query(
    `SELECT *
     FROM tasks
     WHERE
       user_id = $1
       AND task_date = $2`,
    [
      userId,
      yesterday
    ]
  );

  const todayTasks = todayResult.rows;
  const yesterdayTasks = yesterdayResult.rows;

  const stats = calculateStats(todayTasks);
  const yesterdayStats = calculateStats(yesterdayTasks);

  let text =
`📊 **KUNLIK XULOSA**

📅 ${formatUzDate(today)}

━━━━━━━━━━━━━━━━━━━━

👍 **${stats.percent}%**

${progressBar(stats.percent)}

**${stats.completed} / ${stats.total} vazifa bajarildi**

━━━━━━━━━━━━━━━━━━━━

📋 **VAZIFALAR**

`;

  if (todayTasks.length === 0) {

    text += `ℹ️ Bugun uchun vazifalar topilmadi.\n`;

  } else {

    for (const task of todayTasks) {

      const icon =
        task.status === 'completed'
          ? '☑️'
          : task.status === 'failed'
            ? '❌'
            : '⏳';

      text += `${icon} ${task.task_text}\n`;
    }
  }

  text +=
`\n━━━━━━━━━━━━━━━━━━━━

📈 **NATIJA**

🟩 Bajarildi     **${stats.completed}**

🟥 Bajarilmadi   **${stats.failed}**
`;

  if (stats.pending > 0) {
    text += `\n⏳ Belgilanmagan **${stats.pending}**\n`;
  }

  text +=
`
🎯 **${stats.percent}% natija**

${progressBar(stats.percent)}

━━━━━━━━━━━━━━━━━━━━

📅 **KECHA**

`;

  if (yesterdayStats.total === 0) {

    text += `ℹ️ Ma'lumot yo'q\n`;

  } else {

    text +=
`🎯 **${yesterdayStats.percent}%**

${yesterdayStats.completed} / ${yesterdayStats.total} vazifa bajarildi
`;

    const difference =
      stats.percent - yesterdayStats.percent;

    if (difference > 0) {
      text += `\n📈 Kechagiga nisbatan **+${difference}%** yaxshiroq!\n`;
    }

    else if (difference < 0) {
      text += `\n📉 Kechagiga nisbatan **${difference}%** pastroq.\n`;
    }

    else {
      text += `\n➖ Kechagi natija bilan bir xil.\n`;
    }
  }

  const motivation =
    getMotivation(stats.percent);

  text +=
`
━━━━━━━━━━━━━━━━━━━━

💡 **XULOSA**

${motivation.title}

${motivation.text}

🎯 **Kechagi o'zingdan kuchliroq bo'l!**
`;

  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}

// ================================
// /HAFTALIK
// O'TGAN TO'LIQ HAFTA
// ================================

async function handleWeeklyReport(chatId) {

  const userResult = await pool.query(
    `SELECT id
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      "⚠️ User topilmadi."
    );

    return;
  }

  const userId = userResult.rows[0].id;

  const todayString = getTashkentDate();
  const today = new Date(`${todayString}T12:00:00`);

  // Dushanba = 1
  const day =
    today.getDay() === 0
      ? 7
      : today.getDay();

  const currentMonday = new Date(today);
  currentMonday.setDate(
    today.getDate() - day + 1
  );

  const start = new Date(currentMonday);
  start.setDate(
    currentMonday.getDate() - 7
  );

  const end = new Date(currentMonday);
  end.setDate(
    currentMonday.getDate() - 1
  );

  const startDate =
    start.toISOString().slice(0, 10);

  const endDate =
    end.toISOString().slice(0, 10);

  const result = await pool.query(
    `SELECT *
     FROM tasks
     WHERE
       user_id = $1
       AND task_date >= $2
       AND task_date <= $3
     ORDER BY task_date ASC`,
    [
      userId,
      startDate,
      endDate
    ]
  );

  const tasks = result.rows;
  const stats = calculateStats(tasks);

  const days = [
    'Dushanba',
    'Seshanba',
    'Chorshanba',
    'Payshanba',
    'Juma',
    'Shanba',
    'Yakshanba'
  ];

  const dailyStats = {};

  for (let i = 0; i < 7; i++) {

    const date = new Date(start);
    date.setDate(start.getDate() + i);

    const key =
      date.toISOString().slice(0, 10);

    dailyStats[key] = {
      name: days[i],
      completed: 0,
      total: 0,
      percent: 0
    };
  }

  for (const task of tasks) {

    if (!dailyStats[task.task_date]) continue;

    dailyStats[task.task_date].total++;

    if (task.status === 'completed') {
      dailyStats[task.task_date].completed++;
    }
  }

  for (const key in dailyStats) {

    const item = dailyStats[key];

    item.percent =
      item.total > 0
        ? Math.round(
            item.completed / item.total * 100
          )
        : 0;
  }

  let text =
`📊 HAFTALIK XULOSA

📅 ${formatUzDate(startDate)} – ${formatUzDate(endDate)}

━━━━━━━━━━━━━━━━━━━━

🔥 ${stats.percent}%

${progressBar(stats.percent)}

${stats.completed} / ${stats.total} vazifa bajarildi

━━━━━━━━━━━━━━━━━━━━

📆 HAFTA KUNLARI

`;

  let bestDay = null;
  let worstDay = null;
  let activeDays = 0;
  let perfectDays = 0;

  for (const key of Object.keys(dailyStats)) {

    const item = dailyStats[key];

    if (item.total === 0) {

      text += `⬜ ${item.name} — Ma'lumot yo'q\n`;
      continue;
    }

    activeDays++;

    if (item.percent === 100) {
      perfectDays++;
    }

    text +=
      `🟩 ${item.name} — ${item.percent}% (${item.completed}/${item.total})\n`;

    if (
      !bestDay ||
      item.percent > bestDay.percent
    ) {
      bestDay = item;
    }

    if (
      !worstDay ||
      item.percent < worstDay.percent
    ) {
      worstDay = item;
    }
  }

  text +=
`
━━━━━━━━━━━━━━━━━━━━

📈 NATIJA

🟩 Bajarildi     ${stats.completed}

🟥 Bajarilmadi   ${stats.failed}

🎯 ${stats.percent}% natija

${progressBar(stats.percent)}

━━━━━━━━━━━━━━━━━━━━

🏆 ENG YAXSHI KUN

`;

  text += bestDay
    ? `🔥 ${bestDay.name} — ${bestDay.percent}%`
    : `ℹ️ Ma'lumot yo'q`;

  text +=
`

📉 ENG SUST KUN

`;

  text += worstDay
    ? `📉 ${worstDay.name} — ${worstDay.percent}%`
    : `ℹ️ Ma'lumot yo'q`;

  text +=
`

━━━━━━━━━━━━━━━━━━━━

📈 OLDINGI HAFTA

ℹ️ Ma'lumot yo'q

━━━━━━━━━━━━━━━━━━━━

🔥 100% kunlar: ${perfectDays} ta

📆 Faol kunlar: ${activeDays} ta

━━━━━━━━━━━━━━━━━━━━

💡 HAFTA XULOSASI

`;

  if (stats.total === 0) {

    text +=
      `📭 Bu hafta uchun vazifalar topilmadi.`;

  } else {

    const motivation =
      getMotivation(stats.percent);

    text +=
      `${motivation.title}\n\n${motivation.text}`;
  }

  await bot.sendMessage(chatId, text);
}

// ================================
// /OYLIK
// JORIY OY
// ================================

async function handleMonthlyReport(chatId) {

  const userResult = await pool.query(
    `SELECT id
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {
    await bot.sendMessage(chatId, '⚠️ User topilmadi.');
    return;
  }

  const todayString = getTashkentDate();
  const today = new Date(`${todayString}T12:00:00`);

  const start = new Date(
    today.getFullYear(),
    today.getMonth(),
    1
  );

  const startDate =
    start.toISOString().slice(0, 10);

  const userId =
    userResult.rows[0].id;

  const result = await pool.query(
    `SELECT *
     FROM tasks
     WHERE
       user_id = $1
       AND task_date >= $2
       AND task_date <= $3`,
    [
      userId,
      startDate,
      todayString
    ]
  );

  const stats =
    calculateStats(result.rows);

  const monthName = new Intl.DateTimeFormat(
    'uz-UZ',
    {
      month: 'long',
      year: 'numeric'
    }
  ).format(today);

  const motivation =
    getMotivation(stats.percent);

  const text =
`📊 OYLIK XULOSA

📅 ${monthName}

━━━━━━━━━━━━━━━━━━━━

🔥 ${stats.percent}%

${progressBar(stats.percent)}

${stats.completed} / ${stats.total} vazifa bajarildi

━━━━━━━━━━━━━━━━━━━━

📈 NATIJA

🟩 Bajarildi     ${stats.completed}

🟥 Bajarilmadi   ${stats.failed}

⏳ Belgilanmagan ${stats.pending}

━━━━━━━━━━━━━━━━━━━━

💡 OY XULOSASI

${motivation.title}

${motivation.text}

🎯 Har bir kun — yangi imkoniyat!`;

  await bot.sendMessage(chatId, text);
}

// ================================
// /YILLIK
// JORIY YIL
// ================================

async function handleYearlyReport(chatId) {

  const userResult = await pool.query(
    `SELECT id
     FROM users
     WHERE telegram_chat_id = $1`,
    [chatId]
  );

  if (userResult.rows.length === 0) {
    await bot.sendMessage(chatId, '⚠️ User topilmadi.');
    return;
  }

  const today = getTashkentDate();
  const year =
    Number(today.slice(0, 4));

  const startDate =
    `${year}-01-01`;

  const result = await pool.query(
    `SELECT *
     FROM tasks
     WHERE
       user_id = $1
       AND task_date >= $2
       AND task_date <= $3`,
    [
      userResult.rows[0].id,
      startDate,
      today
    ]
  );

  const stats =
    calculateStats(result.rows);

  const motivation =
    getMotivation(stats.percent);

  const text =
`📊 YILLIK XULOSA

📅 ${year}-yil

━━━━━━━━━━━━━━━━━━━━

🔥 ${stats.percent}%

${progressBar(stats.percent)}

${stats.completed} / ${stats.total} vazifa bajarildi

━━━━━━━━━━━━━━━━━━━━

📈 NATIJA

🟩 Bajarildi     ${stats.completed}

🟥 Bajarilmadi   ${stats.failed}

⏳ Belgilanmagan ${stats.pending}

━━━━━━━━━━━━━━━━━━━━

💡 YIL XULOSASI

${motivation.title}

${motivation.text}

🏆 Yangi yil — yangi natijalar!`;

  await bot.sendMessage(chatId, text);
}

// ================================
// /ADMIN
// ================================

async function handleAdmin(chatId) {

  if (!isAdmin(chatId)) {

    await bot.sendMessage(
      chatId,
      '⛔ Sizda admin huquqi yo‘q.'
    );

    return;
  }

  const today =
    getTashkentDate();

  const result = await pool.query(
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
        WHERE created_at >= NOW() - INTERVAL '7 days'
      )::int AS new_users

    FROM users
    `
  );

  const stats =
    result.rows[0];

  const timeResult =
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

  let text =
`🛠 ADMIN PANEL

━━━━━━━━━━━━━━━━━━━━

👥 Jami userlar: ${stats.total_users}

🟢 Faol: ${stats.active_users}

🏁 Yakunlagan: ${stats.completed_users}

🎁 Trial: ${stats.trial_users}

💳 Paid: ${stats.paid_users}

🆕 Oxirgi 7 kunda: ${stats.new_users}

━━━━━━━━━━━━━━━━━━━━

⏰ MASHHUR VAQTLAR

`;

  if (timeResult.rows.length === 0) {

    text +=
      `ℹ️ Ma'lumot yo'q`;

  } else {

    for (
      const item of timeResult.rows
    ) {

      text +=
        `🕐 ${String(item.morning_time).slice(0, 5)} — ${item.count} ta\n`;
    }
  }

  await bot.sendMessage(chatId, text);
}

// ================================
// /XABAR
// FAQAT ADMIN
// ================================

async function handleBroadcast(
  chatId,
  messageText
) {

  if (!isAdmin(chatId)) {

    await bot.sendMessage(
      chatId,
      '⛔ Sizda admin huquqi yo‘q.'
    );

    return;
  }

  let text =
    messageText.replace(
      /^\/xabar\s*/,
      ''
    ).trim();

  if (!text) {

    await bot.sendMessage(
      chatId,
      `📢 Xabar yuborish formati:

/xabar Sizning xabaringiz

Qo'shimcha:

@ism:Shaxboz
@limit:20`
    );

    return;
  }

  let nameFilter = null;
  let limit = null;

  const nameMatch =
    text.match(/@ism:([^\s]+)/);

  if (nameMatch) {

    nameFilter =
      nameMatch[1];

    text = text.replace(
      nameMatch[0],
      ''
    ).trim();
  }

  const limitMatch =
    text.match(/@limit:(\d+)/);

  if (limitMatch) {

    limit =
      Number(limitMatch[1]);

    text = text.replace(
      limitMatch[0],
      ''
    ).trim();
  }

  let query =
    `SELECT id, telegram_chat_id, first_name
     FROM users
     WHERE telegram_chat_id IS NOT NULL`;

  const params = [];

  if (nameFilter) {

    params.push(
      `%${nameFilter}%`
    );

    query +=
      ` AND first_name ILIKE $${params.length}`;
  }

  if (limit) {
    query += ` LIMIT ${limit}`;
  }

  const usersResult =
    await pool.query(
      query,
      params
    );

  const users =
    usersResult.rows;

  let sent = 0;
  let failed = 0;

  for (let i = 0; i < users.length; i++) {

    const user = users[i];

    try {

      await bot.sendMessage(
        user.telegram_chat_id,
        text
      );

      sent++;

    } catch (error) {

      failed++;

      console.error(
        `Broadcast error ${user.telegram_chat_id}:`,
        error.message
      );
    }

    // Har 20 userdan keyin
    if (
      (i + 1) % 20 === 0 &&
      i + 1 < users.length
    ) {

      await new Promise(resolve =>
        setTimeout(resolve, 150)
      );
    }
  }

  await bot.sendMessage(
    chatId,
    `📢 Xabar yuborish yakunlandi.

✅ Yuborildi: ${sent}

❌ Xatolik: ${failed}

👥 Jami: ${users.length}`
  );
}

module.exports = router;
