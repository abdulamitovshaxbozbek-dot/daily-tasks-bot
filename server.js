const express = require('express');
require('dotenv').config();
const pool = require('./db');

const app = express();
app.use(express.json());

// HEALTH CHECK
app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date() });
});

// TELEGRAM WEBHOOK
app.post('/api/telegram', require('./routes/telegram'));

// ERROR HANDLER
app.use((err, req, res, next) => {
  console.error(err);
  res.status(500).json({ error: err.message });
});

// START SERVER
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`✅ Server running on port ${PORT}`);
});
