# Pooling

Pools are **process-local**. Each running application has its own pools. If several processes or servers
connect to the same database, plan their combined `max_size` against the server's `max_connections`, or put
a server-side pooler such as PgBouncer in front.

## Lifecycle of `with connector.connection() as conn:`

1. **Pool lookup:** `PoolManager.acquire(pool_key)`. Connectors with the same identity share one
   reference-counted pool; a different user, tenant, credential or config always gets its own pool.
2. **Checkout:**
   - It reuses the most recently returned idle connection, unless that one is past `max_lifetime` or `idle_timeout`.
   - Otherwise, below `max_size`, it opens a new connection.
   - Otherwise it waits up to `checkout_timeout` and then raises `PoolTimeout`. With `on_exhausted: fail`, it raises `PoolExhausted` immediately instead.
3. **Health check:** a connection not checked for `health_check_interval` runs `SELECT 1` first. A failed
   one is closed and replaced transparently.
4. **Use:** the connection is yours alone until the block exits.
5. **Return:** open or failed transactions are rolled back, and `autocommit`, `isolation_level`, `read_only`
   and `deferrable` are reset. Closed, broken or mid-query connections are discarded instead of reused.

## Settings (`pool:` in connections.yaml, or `pool_config=` in `create`)

| Setting | Default | Meaning |
|---|---|---|
| `min_size` | 0 | Connections kept open and warm (filled by background maintenance) |
| `max_size` | 5 | Hard cap on connections to the database from this pool |
| `checkout_timeout` | 30s | How long a caller waits for a free connection |
| `max_lifetime` | 1800s | Connections older than this are recycled |
| `idle_timeout` | 600s | Idle connections older than this are closed |
| `health_check_interval` | 30s | Re-check before reuse after this long (0 = every checkout) |
| `on_exhausted` | `wait` | `wait` (then `PoolTimeout`) or `fail` (immediate `PoolExhausted`) |
| `shutdown_timeout` | 10s | On close, how long to wait for borrowed connections to be returned |

`maintenance_interval` in connections.yaml (or `manager.start_maintenance(seconds)`) runs a background thread
that recycles stale idle connections, health-checks idle ones and tops up to `min_size`. Without it, recycling
still happens, but only at checkout and return.

## Monitoring

`manager.pool_manager.stats()` returns `PoolStats(total, idle, in_use, pending, waiting, max_size)` per pool.
`manager.add_hook(fn)` receives events such as `connection.established`, `pool.checkout`, `pool.return`,
`pool.recycled` and `pool.exhausted`.
