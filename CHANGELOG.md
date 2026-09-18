# Changelog

Notable changes per release. Versions follow [semantic versioning](https://semver.org),
with the caveat that this is pre-1.0 software: the tool surface is fixed by the
spec, but the on-disk schema may still break without a migration path.

Three version numbers move independently here, and `seshat info` reports all
three: the **software** version below, the **spec** revision it implements
(`docs/seshat-mcp-spec.md`), and the **store schema** version on disk.

## 0.8.0 — 2026-09-18

Initial public release. Implements spec 1.4; store schema 4.
