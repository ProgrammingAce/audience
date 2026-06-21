I am Chip's LLM, responding here. Thanks for the contribution — these are real gaps the codebase had noted (the old TODO comment at the top of `battle.lua` literally listed LOS / A* / block reaction, and `docs/TODOs.md` has an open "Card-aware AI picker" entry). The shape of the work is right; a few concrete problems to address before this lands:

Correctness:
- `find_threatening_bullet` filters by `bullet.owner_handle.id == my_slot` and `bullet.reflect_immune_player_id == my_slot`, but those are ECS entity IDs, not player slots. `owner_handle` is `{id, gen}` from `world:handle_of(player_entity_id)`, and `reflect_immune_player_id` is set to `hit_target.id` in `bullet_collision.lua:257`. They will only coincidentally match a slot when slot==entity-id, which won't hold once you have respawns or more than a couple of entities. Resolve via `world:get(bullet.owner_handle, "Player").slot` (or compare against `self_id` which `find_self` already returns).
- The PR body says "no segment-vs-obb primitive yet" — `engine.physics.swept.segment_vs_obb` exists at `swept.lua:114` and can drop straight into the LOS dispatch.
- `local tdx = math.sqrt(threat_dist_sq)` is dead code (and the comment "dx from bullet to us" is wrong — it's a distance magnitude). LSP will flag it.

Style / conventions:
- This branch adds two new `goto continue` / `::continue::` blocks. Master commit `6538d46e` explicitly removed those with the message "User feedback flagged: never use Lua goto/labels" — please restructure to inverted guards.
- The `find_threatening_bullet` call line is 104 chars; `stylua.toml` has `column_width = 100`. `make format` will fix it.
- `battle.lua` doubles in size (276 added lines). The A* function (~85 LOC) reads `NavGraph` and queries `Nav.find_platform` — it would fit naturally in `src/game/ai/nav.lua` alongside `build_edges` / `find_platform`, keeping pathfinding co-located. The codebase is allergic to code inflation in `src/`; the `battle.lua` module-level concerns should stay narrow.

Tests:
- No new tests for `has_los`, `find_threatening_bullet`, `astar`, or `loadout_bias`. The project enforces TDD, and the existing 26 AI tests don't exercise any of the new paths.
- The "22 pre-existing failures" claim is surprising — master is normally green. Your branch is ~8 commits behind (PR base is `5f25281b`, master is `13a255a3` and includes physics 2c.2c closeout fixes). Please rebase and re-run; if anything still fails I'd want to see the specific names.

Smaller stuff:
- `picker.lua` docstring says "tie-breaks via RNG" as if new, but master already tie-breaks via `ai_rng.random` — please keep the docstring honest about what actually changed.
- `loadout_bias` weights (`-4` / `-2` / `+1`) and `CARD_CATEGORIES` coverage are undocumented; either drop a brief rationale comment or note them as "first-pass, retune from playtest" the way `CARD_SCORES` does.

The `testing/output.lua` Windows `_isatty` fix is solid and worth landing on its own, independent of the rest.

Suggested path: split this into (a) the `output.lua` Windows fix landed quickly, (b) the picker `loadout_bias` work with the docstring corrected, (c) the LOS + dodge + A* work re-scoped with the owner-slot/id bug fixed, goto removed, A* moved to `nav.lua`, and unit tests added (the existing `tests/unit/game_ai_battle_test.lua` and `tests/unit/game_ai_nav_test.lua` are good templates).

