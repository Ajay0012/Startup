# Windows application runtime

`RuntimeBuilder` owns one application adapter, catalog, resolver, and control runtime. The real adapter uses registry, bounded fixed PowerShell discovery queries, process metadata, and user32 window APIs. API responses intentionally omit executable paths and window contents.

The simulated adapter is injected explicitly by tests; production composition never selects it implicitly.
