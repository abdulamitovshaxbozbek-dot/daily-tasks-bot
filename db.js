const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: { rejectUnauthorized: false },
  max: 20,
  min: 5,
  connectionTimeoutMillis: 10000,
  idleTimeoutMillis: 30000
});

module.exports = pool;
