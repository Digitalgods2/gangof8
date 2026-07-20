# Development mode

Prioritize implementing the requested product behavior.

Do not create new tests unless the user explicitly requests them or the change fixes a confirmed regression.

After implementation, run only the smallest existing test or build command needed to detect obvious breakage.

Do not expand test coverage, refactor unrelated code, or pursue unrelated failures.

Existing unrelated test failures do not block completing the requested implementation.

For prototypes, working functionality takes priority over comprehensive validation.

When a task says, "Implement this directly. Spend the majority of the work on production code. Do not add regression tests in this pass. Run only a targeted smoke check after the feature works," follow that task-specific direction exactly.
