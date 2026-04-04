/**
 * PostgreSQL connection pool singleton for server-side use only.
 * Uses node-postgres (pg) with parameterized queries.
 */

import { Pool, QueryResultRow } from "pg";

let pool: Pool | null = null;

function getPool(): Pool {
  if (!pool) {
    pool = new Pool({
      connectionString: process.env.DATABASE_URL,
      max: 10,
      idleTimeoutMillis: 30000,
      connectionTimeoutMillis: 5000,
    });
  }
  return pool;
}

export async function query<T extends QueryResultRow = QueryResultRow>(
  sql: string,
  params: unknown[] = []
): Promise<T[]> {
  const client = getPool();
  const result = await client.query<T>(sql, params);
  return result.rows;
}

export default getPool;
