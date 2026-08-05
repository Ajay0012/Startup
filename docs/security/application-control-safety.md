# Application-control safety

Discovery, resolution, status, and window enumeration are read-only. Open and window-state changes are reversible. Graceful close is moderate risk. Process termination is only exposed through the exact persistent approval boundary; generic shell execution and shell/interpreter launch are rejected.

Window titles are used transiently for identity matching and are never persisted as window contents.
