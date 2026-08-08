# ER AP — frozen contract record (A–F), base-game MVP

**Status: all contract values are decided.** What remains is verification and propagation, not
negotiation (checklist at the end). This is the canonical state across spec-1 (apworld), spec-2
(static randomizer), and spec-3 (runtime client), and **supersedes the original spec's shared-decision
table and the contract-deltas memo.**

## Decision table

| # | Decision | Frozen value | Between | Evidence / status |
|---|---|---|---|---|
| A | Game / login string | `EldenRing` — exact, **no space** | all three | confirmed from the live apworld registration |
| B | `versions` semantics + value | the **encoding-contract** version (single source of truth), enforced by **both** randomizer @ bake **and** client @ connect. Range: lockstep `">=0.1.0-beta.1 <0.1.0-beta.2"` now; graduate to `">=0.1.0 <0.2.0"` once A–F freeze. Discipline: minor = breaking, patch = decode-compatible, 1.0.0 = upstream-stable. | apworld emits ↔ {randomizer, client} enforce | union accepted by both consumers |
| C | Foreign-remove bool | `disableUseAtOutOfColiseum` — **ER paramdef spelling (capital O-F)**, not DS3's `disableUseAtOut`**`of`**`Coliseum` | randomizer ↔ client | dump: col 56 = 2326 zero / **0 set**; `disableUseAtColiseum` col 55 = **78 set**. Both chats recounted, identical. |
| D | Synthetic detection threshold | **goods `> 3,780,000` only.** Weapon/armor thresholds are dead under F. (Fallback, if synthetic weapons were ever reintroduced — which F forbids: weapon `> 99,999,999`.) | randomizer ↔ client | collapsed by F; weapon-threshold bug moot |
| E | Location key scheme | `locationIdsToKeys` values **==** randomizer `StableKey(slot)`; format `<mapId>,<block>:<id>:<aux>:`; resolver is N:1 **forward** per `apId` (no reverse-map) | apworld ↔ randomizer | grammar agreed; **byte-diff pending a build** — the one item that could still reopen E |
| F | Synthetic carrier param (**new**) | **goods-only** — every synthetic placeholder is a clone of the inert goods template **8010**. **Forced**, not preferred. | randomizer ↔ client | dump-confirmed (carrier table below) |

## Why F is forced — param carrier coverage

| field (encoding role) | GOODS | WEAPON | PROTECTOR | ACCESSORY |
|---|:-:|:-:|:-:|:-:|
| `vagrantItemLotId` (loc id low32) | ✅ | — | — | ✅ |
| `vagrantBonusEneDropItemLotId` (loc id high32) | ✅ | — | — | ✅ |
| `basicPrice` (replacement id) | ✅ | — | ✅ | ✅ |
| `disableUseAtOutOfColiseum` (C flag) | ✅ | — | — | — |

Goods is the only table carrying all four. Accessory carries a *local* replacement but not a *foreign*
synthetic (no C flag) → "route talismans through accessory" is foreclosed.

## Synthetic-item encoding (inherited DS3 design, ER-confirmed)

- AP location id → `vagrantItemLotId` (low32) + `vagrantBonusEneDropItemLotId` (high32). **These fields are signed `s32`** (vanilla stores `-1`), so encode/decode must use an *unsigned* recombine — C# decode `((long)(uint)low) | (((long)(uint)high) << 32)`; a naive `(long)low | ((long)high << 32)` sign-extends and corrupts any half with bit 31 set. Reference: spec-2's `vagrant_codec.py` golden vectors. (ER's current ids are all ~7M, so none trip this — see byte-diff note below.)
- local replacement real id → `basicPrice` (all-zero doubles as the "no local item" / foreign default)
- local replacement qty → **`sellValue`** (col 9) — decided by spec-3, **accepted by spec-2**
- foreign-remove flag → `disableUseAtOutOfColiseum` (Decision C)
- synthetic template → goods id **8010**; synthetic rows are clones with the above overwritten

## Apworld emit (validated, base-game)

`slot_data = { options (29 keys), seed, slot, apIdsToItemIds, itemCounts, locationIdsToKeys, versions }`.
`enable_dlc = 0` for the MVP (3,705-location seed; `material_rando` default on, `enemy_rando` default
off; all option booleans are int `0/1`). `locationIdsToKeys` is fully int-keyed (Golden Rune fix).
Emitted `versions = ">=0.1.0-beta.1 <0.1.0-beta.2"`, matching the B baseline.

## Remaining before first build — verification + propagation, no open decisions

1. **Closed — qty → `sellValue`.** spec-2 accepted; randomizer writes `sellValue` paired with `basicPrice`.
2. **Closed — C casing.** spec-2's set-call uses the ER spelling `disableUseAtOutOfColiseum`; the regen pulls
   the ER paramdef name, never DS3's lowercase-f `Outof`.
3. **Closed — F coverage (gate = YES).** Goods is placeable at every surface: item lots
   `lotItemCategory = 1` (4,031 entries), shops `equipType = 3` (498 + recipes), event/script grants via
   their backing lots. Decision D + the weapon threshold are dead.
4. **Open — E byte-diff** *(last build-blocked item).* Once spec-2 bakes the two samples
   (`180000,0:0018007000::`, `111000,0:0000000000:101898:`), the client diffs (a) the `StableKey` string and
   (b) the decoded location-id. **Encoding half de-risked** via spec-2's `vagrant_codec.py` golden vectors.
   ⚠ ER's ids are all ~7M (< 2³¹, high-half zero), so the byte-diff does **not** exercise the s32 sign path —
   a naive (sign-extending) decoder would pass it anyway. The codec unit-test (synthetic bit-31 vectors) is
   the real safeguard for decode sign-safety; port + run it independently of the bake. Passing both closes E
   and freezes A–F → `versions` graduates to `">=0.1.0 <0.2.0"`.
5. **Open — spec-2 wiring.** typed-synthetic `// VERIFY` (nothing inherited emits a typed synthetic for ER),
   `versions` bake-check grep + ER case + contract-version constant, remaining fork greps
   (SlotKey / Forced / slot-enumeration).

## Apworld status: complete

No further apworld changes for the base-game MVP. Its emitted values all match the frozen contract; the
two code changes that landed — the `versions` field and the Golden Rune string-`ap_code` fix — are in
`dfaf9a8+`. DLC remains the separable follow-on (`enable_dlc`/`messmer_kindle*` toggles ship dormant).
