# Adding a connector

Postgres (`connectors/databases/postgres.py`) is the reference. A new connector is one module plus a registry line.

1. **Config:** a frozen, keyword-only `ConnectionConfig` subclass holding non-secret settings only. Override
   `validate()` (call `super().validate()`), using helpers from `utils/validation.py`.
2. **Connector:** subclass `BaseConnector[YourConfig, NativeClient]` and set `name` (lowercase registry key),
   `category` (`databases | warehouses | cloud | saas | apis`), `config_class` and `credential_types`.
   Implement only the hooks:
   - `_create_native(credentials)`: open one native client. Import the driver with
     `import_driver("module", extra="your-extra")`, set an explicit timeout, and pass secrets as arguments,
     never in a DSN string you build.
   - `_ping(native)`: a cheap liveness check that reads no data.
   - optional `_reset_native` (clean per-caller state before reuse; return False to discard),
     `_close_native`, `_validate_credentials`.
   Do **not** override the public lifecycle methods, and never add fetch, list, read, paginate or transform methods.
   The contract tests fail if you do.
3. **Register** it in `registry/connector_registry.py` (`BUILTIN_CONNECTORS`) as `"module:Class"`, and add the
   driver as an optional extra in `pyproject.toml`.
4. **Tests:**
   - unit tests with a stubbed driver (see `tests/unit/test_postgres.py`)
   - the contract suite in `tests/unit/contract/`, which runs automatically for registered connectors
   - security tests
   - an integration test that is skipped without its environment variables
5. **Harness and flow chart:** add an entry for the new type to `connections.yaml` (or `connector_tests/connectors_test.yaml`), and add its flow to `connector_tests/flows.yaml`.
6. **Record it:** note the new connector, its extra and any design decisions in the README and docs.

Driver errors are mapped by `map_driver_error` (message classification). If a source has a failure mode that
classification gets wrong, handle it in the connector and raise the right SDK exception.
