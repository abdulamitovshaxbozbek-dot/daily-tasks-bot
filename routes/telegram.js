const express = require('express');
const router = express.Router();
const pool = require('../server');
const TelegramAPI = require('node-telegram-bot-api');

const bot = new TelegramAPI(process.env.TELEGRAM_TOKEN);

router.post('/', async (req, res) => {
  try {
    const trigger = req.body;

    let route = 'other';
    const chatId = trigger.message?.chat?.id || trigger.callback_query?.message?.chat?.id;
    const messageText = (trigger.message?.text || '').trim();
    const callbackData = trigger.callback_query?.data || null;
    const username = trigger.message?.chat?.username || trigger.callback_query?.from?.username || '';
    const firstName = trigger.message?.chat?.first_name || trigger.callback_query?.from?.first_name || "Do'st";

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
    } else if (messageText && !messageText.startsWith('/')) {
      route = 'task_text';
    }

    const start = Date.now();

    switch(route) {
      case 'start':
        await handleStart(chatId, firstName, username);
        break;
      case 'task_text':
        await handleTaskWrite(chatId, messageText);
        break;
      case 'task_status':
        await handleTaskStatus(callbackData, trigger.callback_query.id, trigger.callback_query.message);
        break;
      case 'morning_time':
        await handleMorningTime(chatId, callbackData, trigger.callback_query.id);
        break;
      case 'hisobot':
        await handleReport(chatId, 'daily');
        break;
      case 'haftalik':
        await handleReport(chatId, 'weekly');
        break;
      case 'oylik':
        await handleReport(chatId, 'monthly');
        break;
    }

    const duration = Date.now() - start;
    console.log(`[${route}] ${chatId} - ${duration}ms`);

    res.json({ ok: true, duration });
  } catch (err) {
    console.error('Error:', err);
    res.status(500).json({ error: err.message });
  }
});

async function handleStart(chatId, firstName, username) {
  const user = await pool.query(
    'SELECT id FROM users WHERE telegram_chat_id = $1',
    [chatId]
  );

  if (user.rows.length > 0) {
    await bot.sendMessage(chatId,
      `👋 Assalomu alaykum, ${firstName}!\n\nSiz allaqachon ro'yxatdan o'tgansiz. ✅\n\n📋 Vazifalaringizni yuborishni davom ettirishingiz mumkin.`
    );
    return;
  }

  await pool.query(
    `INSERT INTO users (telegram_chat_id, telegram_username, first_name, state)
     VALUES ($1, $2, $3, $4)`,
    [chatId, username, firstName, 'waiting_morning_time']
  );

  await bot.sendMessage(chatId,
    `👋 Assalomu alaykum, ${firstName}!\n\n🌅 Ertalab qaysi vaqtda vazifalaringizni so'raylik?`,
    {
      reply_markup: {
        inline_keyboard: [
          [
            { text: '07:00', callback_data: 'morning_time|07:00' },
            { text: '08:00', callback_data: 'morning_time|08:00' },
            { text: '09:00', callback_data: 'morning_time|09:00' }
          ]
        ]
      }
    }
  );
}

async function handleTaskWrite(chatId, text) {
  const user = await pool.query(
    'SELECT id, state FROM users WHERE telegram_chat_id = $1',
    [chatId]
  );

  if (user.rows.length === 0 || user.rows[0].state !== 'active') {
    await bot.sendMessage(chatId, '⚠️ Avval /start buyrug\'ini bering.');
    return;
  }

  const userId = user.rows[0].id;
  const today = new Date().toISOString().split('T')[0];

  const taskLines = text
    .split('\n')
    .map(t => t.trim())
    .filter(t => t.length > 0);

  if (taskLines.length === 0) {
    await bot.sendMessage(chatId, '⚠️ Vazifa matni bo\'sh.');
    return;
  }

  let addedCount = 0;
  let duplicateCount = 0;

  for (const taskText of taskLines) {
    const existCheck = await pool.query(
      `SELECT id FROM tasks
       WHERE user_id = $1 AND task_date = $2 AND task_text = $3`,
      [userId, today, taskText]
    );

    if (existCheck.rows.length > 0) {
      duplicateCount++;
      continue;
    }

    await pool.query(
      `INSERT INTO tasks (user_id, task_date, task_text, status)
       VALUES ($1, $2, $3, $4)`,
      [userId, today, taskText, 'pending']
    );
    addedCount++;
  }

  let message = '';
  if (addedCount > 0) {
    message += `🎉 ${addedCount} ta yangi vazifa qabul qilindi!\n`;
  }
  if (duplicateCount > 0) {
    message += `🔄 ${duplicateCount} ta vazifa bugun allaqachon qo'shilgan.\n`;
  }
  if (!message) {
    message = '✅ Vazifalar saqlandi!';
  }

  message += '\n🤲 Kuningiz barakatli o\'tsin!\n\n🏁 Kuningizni yakunlaganingizda /hisobot bering.';

  await bot.sendMessage(chatId, message);
}

async function handleTaskStatus(callbackData, callbackQueryId, message) {
  const [_, status, taskId] = callbackData.split('|');

  await pool.query(
    'UPDATE tasks SET status = $1 WHERE id = $2',
    [status, taskId]
  );

  const statusEmoji = status === 'completed' ? '✅' : '❌';
  await bot.answerCallbackQuery(callbackQueryId, `${statusEmoji} Saqlandi!`);

  try {
    await bot.deleteMessage(message.chat.id, message.message_id);
  } catch (e) {
    console.log('Message delete failed');
  }
}

async function handleMorningTime(chatId, callbackData, callbackQueryId) {
  const time = callbackData.split('|')[1];

  await pool.query(
    `UPDATE users SET morning_time = $1, state = $2
     WHERE telegram_chat_id = $3`,
    [time, 'active', chatId]
  );

  await bot.answerCallbackQuery(callbackQueryId);
  await bot.sendMessage(chatId,
    `✅ Ertalabki vaqt belgilandi: ${time}\n\n🚀 Endi vazifalaringizni yuborishingiz mumkin!\n\n🤲 Kuningiz barakatli o'tsin!`
  );
}

async function handleReport(chatId, type) {
  const user = await pool.query(
    'SELECT id FROM users WHERE telegram_chat_id = $1',
    [chatId]
  );

  if (user.rows.length === 0) {
    await bot.sendMessage(chatId, '⚠️ User topilmadi.');
    return;
  }

  const userId = user.rows[0].id;
  let dateFilter = '';

  if (type === 'daily') {
    const today = new Date().toISOString().split('T')[0];
    dateFilter = `AND task_date = '${today}'`;
  } else if (type === 'weekly') {
    const weekAgo = new Date();
    weekAgo.setDate(weekAgo.getDate() - 7);
    const weekAgoStr = weekAgo.toISOString().split('T')[0];
    const today = new Date().toISOString().split('T')[0];
    dateFilter = `AND task_date >= '${weekAgoStr}' AND task_date <= '${today}'`;
  } else if (type === 'monthly') {
    const monthAgo = new Date();
    monthAgo.setMonth(monthAgo.getMonth() - 1);
    const monthAgoStr = monthAgo.toISOString().split('T')[0];
    const today = new Date().toISOString().split('T')[0];
    dateFilter = `AND task_date >= '${monthAgoStr}' AND task_date <= '${today}'`;
  }

  const result = await pool.query(
    `SELECT status, COUNT(*) as count FROM tasks
     WHERE user_id = $1 ${dateFilter}
     GROUP BY status`,
    [userId]
  );

  const stats = result.rows.reduce((acc, row) => {
    acc[row.status] = parseInt(row.count);
    return acc;
  }, { completed: 0, failed: 0, pending: 0 });

  const total = stats.completed + stats.failed + stats.pending;
  const percent = total > 0 ? Math.round((stats.completed / total) * 100) : 0;

  const typeLabel = type === 'daily' ? 'KUNLIK' : type === 'weekly' ? 'HAFTALIK' : 'OYLIK';

  let text = `📊 ${typeLabel} XULOSA\n\n`;
  text += `🎯 ${percent}%\n\n`;
  text += `✅ ${stats.completed}/${total} vazifa bajarildi\n`;
  text += `❌ ${stats.failed} bajarilmadi\n`;
  if (stats.pending > 0) text += `⏳ ${stats.pending} belgilanmagan\n`;

  await bot.sendMessage(chatId, text, { parse_mode: 'Markdown' });
}

module.exports = router;
