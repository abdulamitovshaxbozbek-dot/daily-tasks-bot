const express = require('express');
require('dotenv').config();
require('./db');

const app = express();

app.use(express.json());

// HEALTH CHECK
app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    timestamp: new Date()
  });
});

// TELEGRAM WEBHOOK
const telegramRouter = require('./routes/telegram');
app.use('/api/telegram', telegramRouter);

// ERROR HANDLER
app.use((err, req, res, next) => {
  console.error('ERROR:', err);
  res.status(500).json({ error: err.message });
});

// 404 HANDLER
app.use((req, res) => {
  console.log('404:', req.method, req.url);

  res.status(404).json({
    error: 'Not found',
    method: req.method,
    url: req.url
  });
});

const PORT = process.env.PORT || 3000;

// Server faqat node server.js bilan ishga tushirilganda listen qiladi
if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`✅ Server running on port ${PORT}`);
  });
}

// Test qilish uchun Express app'ni export qilamiz
module.exports = app;
