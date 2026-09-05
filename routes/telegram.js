const express = require('express');
const router = express.Router();
const pool = require('../db');
const TelegramAPI = require('node-telegram-bot-api');

const bot = new TelegramAPI(process.env.TELEGRAM_TOKEN);

/*
========================================
TELEGRAM WEBHOOK
POST /api/telegram
========================================
*/

router.post('/', async (req, res) => {
  try {
    const trigger = req.body;

    let route = 'other';

    const chatId =
      trigger.message?.chat?.id ||
      trigger.callback_query?.message?.chat?.id;

    const messageText =
      (trigger.message?.text || '').trim();

    const callbackData =
      trigger.callback_query?.data || null;

    const username =
      trigger.message?.chat?.username ||
      trigger.callback_query?.from?.username ||
      '';

    const firstName =
      trigger.message?.chat?.first_name ||
      trigger.callback_query?.from?.first_name ||
      "Do'st";

    /*
    ========================================
    QAYSI ROUTE EKANINI ANIQLASH
    ========================================
    */

    if (messageText === '/start') {
      route = 'start';

    } else if (messageText === '/hisobot') {
      route = 'hisobot';

    } else if (messageText === '/haftalik') {
      route = 'haftalik';

    } else if (messageText === '/oylik') {
      route = 'oylik';

    } else if (callbackData?.startsWith('task_status|')) {
      route = 'task_status';

    } else if (callbackData?.startsWith('morning_time|')) {
      route = 'morning_time';

    } else if (
      messageText &&
      !messageText.startsWith('/')
    ) {
      route = 'task_text';
    }

    const start = Date.now();

    /*
    ========================================
    ROUTING
    ========================================
    */

    switch (route) {

      case 'start':
        await handleStart(
          chatId,
          firstName,
          username
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
          callbackData,
          trigger.callback_query.id,
          trigger.callback_query.message
        );
        break;

      case 'morning_time':
        await handleMorningTime(
          chatId,
          callbackData,
          trigger.callback_query.id
        );
        break;

      case 'hisobot':
        await handleReport(
          chatId,
          'daily'
        );
        break;

      case 'haftalik':
        await handleReport(
          chatId,
          'weekly'
        );
        break;

      case 'oylik':
        await handleReport(
          chatId,
          'monthly'
        );
        break;
    }

    const duration = Date.now() - start;

    console.log(
      `[${route}] ${chatId} - ${duration}ms`
    );

    res.json({
      ok: true,
      duration
    });

  } catch (err) {

    console.error('Error:', err);

    res.status(500).json({
      error: err.message
    });
  }
});


/*
========================================
/START
========================================
*/

async function handleStart(
  chatId,
  firstName,
  username
) {

  const user = await pool.query(
    'SELECT id FROM users WHERE telegram_chat_id = $1',
    [chatId]
  );

  /*
  AGAR USER OLDIN RO'YXATDAN O'TGAN BO'LSA
  */

  if (user.rows.length > 0) {

    await bot.sendMessage(
      chatId,
      `👋 Assalomu alaykum, ${firstName}!

Siz allaqachon ro'yxatdan o'tgansiz. ✅

📋 Vazifalaringizni yuborishni davom ettirishingiz mumkin.`
    );

    return;
  }

  /*
  YANGI USER
  */

  await pool.query(
    `
    INSERT INTO users (
      telegram_chat_id,
      telegram_username,
      first_name,
      state
    )
    VALUES ($1, $2, $3, $4)
    `,
    [
      chatId,
      username,
      firstName,
      'waiting_morning_time'
    ]
  );

  /*
  ERTALABKI VAQTNI TANLASH

  02:00 dan 10:00 gacha
  */

  await bot.sendMessage(
    chatId,
    `👋 Assalomu alaykum, ${firstName}!

🌅 Ertalab qaysi vaqtda vazifalaringizni so'raylik?`,
    {
      reply_markup: {
        inline_keyboard: [

          [
            {
              text: '02:00',
              callback_data: 'morning_time|02:00'
            },
            {
              text: '03:00',
              callback_data: 'morning_time|03:00'
            },
            {
              text: '04:00',
              callback_data: 'morning_time|04:00'
            }
          ],

          [
            {
              text: '05:00',
              callback_data: 'morning_time|05:00'
            },
            {
              text: '06:00',
              callback_data: 'morning_time|06:00'
            },
            {
              text: '07:00',
              callback_data: 'morning_time|07:00'
            }
          ],

          [
            {
              text: '08:00',
              callback_data: 'morning_time|08:00'
            },
            {
              text: '09:00',
              callback_data: 'morning_time|09:00'
            },
            {
              text: '10:00',
              callback_data: 'morning_time|10:00'
            }
          ]

        ]
      }
    }
  );
}


/*
========================================
VAZIFA YOZISH
========================================
*/

async function handleTaskWrite(
  chatId,
  text
) {

  const user = await pool.query(
    `
    SELECT id, state
    FROM users
    WHERE telegram_chat_id = $1
    `,
    [chatId]
  );

  /*
  USER YO'Q YOKI HALI ACTIVE EMAS
  */

  if (
    user.rows.length === 0 ||
    user.rows[0].state !== 'active'
  ) {

    await bot.sendMessage(
      chatId,
      '⚠️ Avval /start buyrug\'ini bering.'
    );

    return;
  }

  const userId = user.rows[0].id;

  const today =
    new Date()
      .toISOString()
      .split('T')[0];

  /*
  BIR XABARDA BIR NECHTA VAZIFA
  */

  const taskLines = text
    .split('\n')
    .map(t => t.trim())
    .filter(t => t.length > 0);

  if (taskLines.length === 0) {

    await bot.sendMessage(
      chatId,
      '⚠️ Vazifa matni bo\'sh.'
    );

    return;
  }

  let addedCount = 0;
  let duplicateCount = 0;

  /*
  HAR BIR VAZIFANI SAQLASH
  */

  for (const taskText of taskLines) {

    const existCheck =
      await pool.query(
        `
        SELECT id
        FROM tasks
        WHERE user_id = $1
        AND task_date = $2
        AND task_text = $3
        `,
        [
          userId,
          today,
          taskText
        ]
      );

    /*
    DUPLICATE BO'LSA
    */

    if (existCheck.rows.length > 0) {

      duplicateCount++;

      continue;
    }

    /*
    YANGI VAZIFA
    */

    await pool.query(
      `
      INSERT INTO tasks (
        user_id,
        task_date,
        task_text,
        status
      )
      VALUES ($1, $2, $3, $4)
      `,
      [
        userId,
        today,
        taskText,
        'pending'
      ]
    );

    addedCount++;
  }

  /*
  USERGA NATIJA
  */

  let message = '';

  if (addedCount > 0) {

    message +=
      `🎉 ${addedCount} ta yangi vazifa qabul qilindi!\n`;
  }

  if (duplicateCount > 0) {

    message +=
      `🔄 ${duplicateCount} ta vazifa bugun allaqachon qo'shilgan.\n`;
  }

  if (!message) {

    message =
      '✅ Vazifalar saqlandi!';
  }

  message +=
    '\n🤲 Kuningiz barakatli o\'tsin!\n\n🏁 Kuningizni yakunlaganingizda /hisobot bering.';

  await bot.sendMessage(
    chatId,
    message
  );
}


/*
========================================
VAZIFA STATUSINI O'ZGARTIRISH

callback_data:
task_status|completed|TASK_ID

yoki

task_status|failed|TASK_ID
========================================
*/

async function handleTaskStatus(
  callbackData,
  callbackQueryId,
  message
) {

  const [
    _,
    status,
    taskId
  ] = callbackData.split('|');

  await pool.query(
    `
    UPDATE tasks
    SET status = $1
    WHERE id = $2
    `,
    [
      status,
      taskId
    ]
  );

  const statusEmoji =
    status === 'completed'
      ? '✅'
      : '❌';

  await bot.answerCallbackQuery(
    callbackQueryId,
    `${statusEmoji} Saqlandi!`
  );

  /*
  ESKI XABARNI O'CHIRISH
  */

  try {

    await bot.deleteMessage(
      message.chat.id,
      message.message_id
    );

  } catch (e) {

    console.log(
      'Message delete failed'
    );
  }
}


/*
========================================
ERTALABKI VAQTNI SAQLASH
========================================
*/

async function handleMorningTime(
  chatId,
  callbackData,
  callbackQueryId
) {

  const time =
    callbackData.split('|')[1];

  /*
  USER ACTIVE HOLATGA O'TADI
  */

  await pool.query(
    `
    UPDATE users
    SET
      morning_time = $1,
      state = $2
    WHERE telegram_chat_id = $3
    `,
    [
      time,
      'active',
      chatId
    ]
  );

  await bot.answerCallbackQuery(
    callbackQueryId
  );

  await bot.sendMessage(
    chatId,
    `✅ Ertalabki vaqt belgilandi: ${time}

🚀 Endi vazifalaringizni yuborishingiz mumkin!

🤲 Kuningiz barakatli o'tsin!`
  );
}


/*
========================================
HISOBOTLAR

daily
weekly
monthly
========================================
*/

async function handleReport(
  chatId,
  type
) {

  const user =
    await pool.query(
      `
      SELECT id
      FROM users
      WHERE telegram_chat_id = $1
      `,
      [chatId]
    );

  /*
  USER TOPILMADI
  */

  if (user.rows.length === 0) {

    await bot.sendMessage(
      chatId,
      '⚠️ User topilmadi.'
    );

    return;
  }

  const userId =
    user.rows[0].id;

  let dateFilter = '';

  /*
  BUGUNGI HISOBOT
  */

  if (type === 'daily') {

    const today =
      new Date()
        .toISOString()
        .split('T')[0];

    dateFilter =
      `AND task_date = '${today}'`;
  }


  /*
  OXIRGI 7 KUN
  */

  else if (type === 'weekly') {

    const weekAgo =
      new Date();

    weekAgo.setDate(
      weekAgo.getDate() - 7
    );

    const weekAgoStr =
      weekAgo
        .toISOString()
        .split('T')[0];

    const today =
      new Date()
        .toISOString()
        .split('T')[0];

    dateFilter =
      `AND task_date >= '${weekAgoStr}'
       AND task_date <= '${today}'`;
  }


  /*
  OXIRGI 1 OY
  */

  else if (type === 'monthly') {

    const monthAgo =
      new Date();

    monthAgo.setMonth(
      monthAgo.getMonth() - 1
    );

    const monthAgoStr =
      monthAgo
        .toISOString()
        .split('T')[0];

    const today =
      new Date()
        .toISOString()
        .split('T')[0];

    dateFilter =
      `AND task_date >= '${monthAgoStr}'
       AND task_date <= '${today}'`;
  }


  /*
  STATISTIKA
  */

  const result =
    await pool.query(
      `
      SELECT
        status,
        COUNT(*) as count

      FROM tasks

      WHERE user_id = $1
      ${dateFilter}

      GROUP BY status
      `,
      [userId]
    );


  const stats =
    result.rows.reduce(
      (acc, row) => {

        acc[row.status] =
          parseInt(row.count);

        return acc;

      },
      {
        completed: 0,
        failed: 0,
        pending: 0
      }
    );


  const total =
    stats.completed +
    stats.failed +
    stats.pending;


  const percent =
    total > 0
      ? Math.round(
          (
            stats.completed /
            total
          ) * 100
        )
      : 0;


  const typeLabel =
    type === 'daily'
      ? 'KUNLIK'
      : type === 'weekly'
        ? 'HAFTALIK'
        : 'OYLIK';


  /*
  HISOBOT MATNI
  */

  let text =
    `📊 ${typeLabel} XULOSA\n\n`;

  text +=
    `🎯 ${percent}%\n\n`;

  text +=
    `✅ ${stats.completed}/${total} vazifa bajarildi\n`;

  text +=
    `❌ ${stats.failed} bajarilmadi\n`;

  if (stats.pending > 0) {

    text +=
      `⏳ ${stats.pending} belgilanmagan\n`;
  }


  await bot.sendMessage(
    chatId,
    text,
    {
      parse_mode: 'Markdown'
    }
  );
}


/*
========================================
EXPORT
========================================
*/

module.exports = router;
