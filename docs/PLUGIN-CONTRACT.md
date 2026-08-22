# Private plugin contract

## Loading

Strategies are loaded through `module:factory`. The factory accepts a decoded
JSON object. Keep the module in an ignored directory or a separate private
package; never copy it into this repository.

## Ordering

1. Raw events are drained from source adapters.
2. The raw batch is committed to its UTC partition.
3. Only then is the identical immutable batch queued to the plugin.
4. Plugin records are written to a separate database.

The single FIFO worker preserves callback order. Plugin work never runs on the
raw persistence thread.

## Lifecycle

- `start(context)` runs once.
- `replace_instruments(instruments, at=...)` receives the explicit catalog.
- `observe(events)` receives already-persisted public events.
- `tick(at=..., source_health=...)` advances timers using caller time.
- `finish(status=..., at=...)` flushes bounded state.

Every callback returns zero or more `StrategyRecord` values. Record payloads are
opaque JSON objects. The infrastructure does not assign trading meaning.

## Failure behavior

- Source failure, overflow, or dropped events fails capture closed.
- Plugin exception or backlog marks only the plugin failed.
- Raw partitions contain no derived records.
- Plugin output contains no raw-table writes.
- A bounded finish timeout prevents shutdown from hanging indefinitely.

