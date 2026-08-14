# STATE — signal-hub
## Intent
Signal/trend/noise capture layer that triggers aesop tasks (design: conductor3/plans/
signal-hub-design.md, approved 2026-08-13). MVP: nflverse schedules + player stats +
trend-indicator orchestration; 3 rules (R001 game-final-win, R002 big-stat-line,
R003 demand-delta); queue consumed by aesop sessions.
## Phase
BUILD — lane fleet L1-L6 parallel, L7 integration merges last.
## NEXT STEPS
1. Lanes land -> merge train with test proof -> L7 integration -> task installer.
2. Register scheduled task (user-visible change, announce), ECOSYSTEM.md row.
3. Preseason dry-run week: --include-preseason, verify events flow, no task fires.
