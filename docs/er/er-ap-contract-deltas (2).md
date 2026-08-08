# ER AP — contract deltas to propagate (from the spec-2 / static-randomizer chat)

The spec-2 work changed or pinned several values in the shared contract. Below: the **updated
master decision table**, then two **copy-paste memos** — one for the spec-1 (apworld) chat, one
for the spec-3 (client) chat. Paste each memo into its chat verbatim; they're written to stand
alone.

---

## Updated shared-decision table (A–F)

Changes from the original spec are flagged **CHANGED** / **NEW** / **RESOLVED**.

| # | Decision | Value | Must agree between |
|---|---|---|---|
| A | Game / login string | `EldenRing` (exact, **no space**) — also the player's YAML `game:` key | apworld ↔ randomizer ↔ client |
| B | `slot_data` `versions` | **contract** version, not a binary version; lockstep `">=0.1.0-beta.N <0.1.0-beta.(N+1)"` until the contract freezes, then graduate to `">=0.1.0 <0.2.0"` | apworld emits ↔ **{randomizer @ bake, client @ connect}** enforce |
| C | Foreign-remove bool | **`disableUseAtOutOfColiseum`** — **RESOLVED** (78 vs 0; spec-3 switches off `disableUseAtColiseum`) | randomizer ↔ client |
| D | Synthetic-ID thresholds | **goods `> 3,780,000` only** (goods-only routing); weapon/armor thresholds **dead** | randomizer ↔ client |
| E | Location key scheme | **PINNED:** `"{mapId:D6},0:{lotId:D10}::"` (pickup) / `"{mapId:D6},0:0000000000:{shopId:D6}:"` (shop). Values **non-unique by design** (shops back many checks) | apworld ↔ randomizer |
| F | Synthetic routing | **goods-only** — every synthetic placeholder is an `EquipParamGoods` row — **NEW** | randomizer ↔ client |

**F (goods-only) — new architectural pin, drives B & D.** spec-3 established and the dump confirms
that goods is the **only** equip table carrying the full encoding surface: weapon lacks the vagrant
pair **and** `basicPrice`; protector lacks the vagrant pair; accessory has the location-id +
replacement fields but **not** `disableUseAtOutOfColiseum` (so it can't hold a *foreign* synthetic).
Even improvising fails — weapon has essentially one clean spare u32 (`wanderingEquipId`) vs the 2–3
the encoding needs. So 100% of synthetics route through goods. **Gate answered (spec-2):** goods is
placeable at every location type — item lots (`lotItemCategory = 1`), shops (`equipType = 3`),
event/script grants (via their backing lots). **Bake change:** DS3 places synthetics
*category-matched*; ER must **force category = goods at the location** (`lotItemCategory=1` /
`equipType=3`, not the vanilla item's category). The client simplifies — one detection path,
weapon/armor masks + thresholds dropped.

**D collapsed by F.** With no synthetic weapon/armor rows, the client detects synthetics by the
**goods threshold alone (`> 3,780,000`)**; the weapon/armor thresholds are dead and the
weapon-threshold bug is moot. (The corrected `> 99,999,999` is preserved only as the answer *if*
synthetic weapons were ever reintroduced — which F forbids.)

**B reconciled (two corrections).** Original "client enforces" was wrong → spec-1: randomizer
enforces at bake (inherited DS3). spec-3 then upgraded to a **union**: randomizer @ bake **and**
client @ connect, because a valid bake doesn't prove the *connecting* client is on the same
contract. `versions` is reframed as the shared **contract** version. Range: **lockstep now** (the
contract is still moving — F/D just changed, qty + StableKey-diff still open), graduate to bounded
`">=0.1.0 <0.2.0"` once it freezes. Nothing has shipped, so the corrected C/D/E + F **are** the
baseline (= `beta.1`); graduation to a released `0.1.0` is the freeze point. Discipline: contract
change (any A–F shift) → bump. **Both checkers must be pre-release-aware and share node-semver
semantics** (spec-3 verified + pinned an acceptance vector): a lib that excludes pre-releases would
reject the valid `0.1.0-beta.1`, and if the bake check and connect check disagree you get a
bake-accept/connect-reject split-brain. .NET side: a node-semver port (`SemanticVersioning`), not
`NuGet.Versioning`; C++ side spec-3 implements the pre-release-in-range rule explicitly. No blanket
`includePrerelease`.

**E pinned by spec-1.** Format above; `mapId` = `mAA_BB_CC` → `AABBCC`, `0` block = DLC reserve,
trailing field empty. Literals are hardcoded from nex3's ER slot defs (same lineage as the
randomizer's `StableKey`) — verify by diffing one pickup + one shop slot. **Resolve forward, per
`apId`** (`key → SlotKey`); never build a `key → apId` reverse map (collapses shop inventories).

**C resolved.** ER's `EquipParamGoods` has **both** Coliseum fields; the decider is the vanilla
count (shared dump, 2,326 goods, confirmed by *both* chats): `disableUseAtColiseum` (col 55) = **78
set** (spec-3's relayed pick → false-positives 78 real items), `disableUseAtOutOfColiseum` (col 56) =
**0 set** ← use this. spec-3 switched. **Spelling gotcha:** the ER paramdef label is
`disableUseAtOutOfColiseum` (capital **O-F**, "OutOf"); the DS3 struct's `disableUseAtOutofColiseum`
(lowercase **f**) is a different label — both sides must use the ER name or they mask different bits.

**Encoding note — qty field RESOLVED → `sellValue`.** Local replacement = `basicPrice` (real id) +
`sellValue` (qty), inherited from DS3 (spec-3's pick over `saleValue`, for parity). Vanilla
distribution is irrelevant to correctness — synthetics are new rows the randomizer writes, and the
client reads qty only on already-classified synthetics. `basicPrice = 0` doubles as the clean "no
local item" default on foreign checks. `saleValue` is the fallback only if the game mutates
`sellValue` on an in-inventory synthetic before swap (no evidence). Randomizer writes `sellValue`.

**Encoding note — vagrant fields are SIGNED `s32` (recombine unsigned).** The int64 location-id rides
in `vagrantItemLotId` (low32) + `vagrantBonusEneDropItemLotId` (high32), and the dump shows both are
**signed** (vanilla `-1`). A half with bit 31 set reads back negative; the client must recombine
`((long)(uint)low) | ((long)(uint)high << 32)`, not the sign-extending `(long)low | ((long)high <<
32)`. Realistic AP ids (large base + index) hit this. Golden codec + test vectors: `vagrant_codec.py`
— the Decision-E byte-diff's shared oracle. Randomizer↔client interop, not apworld.

---

## Memo → spec-1 (apworld) chat

> **STATUS: answered by spec-1.** All four items addressed (key format pinned = Decision E; `0/1`
> encoding + defaults clarified; `versions` reframed to randomizer-enforced; a string-keyed
> `locationIdsToKeys` bug fixed → pull `dfaf9a8`+). Kept here for the record; the live response is
> in the spec-2→spec-1 reply memo. Original text below.
>
> **From the spec-2 (static-randomizer) chat.** The randomizer now consumes your `slot_data` /
> `options` in specific ways. Four things to confirm or pin on the apworld side:
>
> 1. **`options` must contain `material_rando` and `enemy_rando` (booleans).** The randomizer
>    reads `archiOptions["material_rando"]` / `["enemy_rando"]` directly to gate the ER material
>    and enemy passes; if either key is absent the bake **throws at runtime**. Keep
>    `enable_dlc = false` for the base-game MVP (already true for the validated 3,705-location
>    seed; DLC is a separate track).
>
> 2. **NEW contract — location key scheme (Decision E).** The randomizer resolves each AP
>    location by `locationIdsToKeys[apId]` → a key string → its own canonical slot, matched via
>    `StableKey(slot)`. So **the string values you put in `locationIdsToKeys` must be exactly what
>    the randomizer's `StableKey` produces** for the same slot. You own `locationIdsToKeys`, so we
>    need to pin the representation jointly — annotation slot name? lot id? a composite? If the
>    schemes disagree, **every location resolves to nothing and the bake comes out all-vanilla**
>    with no error. This is the highest-risk untested contract between us; document the exact key
>    format and we'll match it. (The randomizer fails loud if resolution is incomplete, so a
>    mismatch will surface immediately at bake time, not silently — but only once both sides exist.)
>
> 3. **Decision A — `game = "EldenRing"`, exact, no space.** The randomizer's login dispatch is
>    now hardcoded to this string; any drift (space, casing) breaks connection.
>
> 4. **Decision B — `versions`** is still the placeholder `>=0.1.0`. Finalize the real value
>    before either side ships. *(Resolved in reply: the **static randomizer** enforces this at bake
>    time per inherited DS3 design — not the client; apworld now emits `">=0.1.0 <0.2.0"`.)*
>
> FYI (no action): Decision D's weapon threshold changed to `> 99,999,999`. It doesn't constrain
> you — you emit real ER item ids, which are all below it — but it's a shared value, noted for
> consistency.

---

## Memo → spec-3 (runtime client) chat

> **From the spec-2 (static-randomizer) chat.** The randomizer bakes synthetic items the way the
> inherited DS3 design specifies, with two ER-specific values that **changed from the spec** and
> one encoding ambiguity to resolve. The client must match all of these to decode correctly:
>
> 1. **Decision C — read `disableUseAtOutOfColiseum` as the foreign-remove flag** (NOT
>    `disableUseAtColiseum`). The spec's "ER has no Coliseum field" is wrong — ER has both. The
>    randomizer sets `disableUseAtOutOfColiseum = 1` on foreign synthetic goods. It's **all-zero
>    across all 2,326 vanilla goods**, so reading it gives zero false positives.
>
> 2. **Decision D — weapon synthetic threshold is `> 99,999,999` (100,000,000), NOT 23,010,000.**
>    This is the **critical client-side fix**: you detect synthetic items by id-over-threshold per
>    category, and real ER base weapons reach **99,060,000** (174 distinct weapon drops above
>    23.01M, looted to 68.51M). The old value would flag a real weapon as synthetic the moment a
>    player picks one up. Full set:
>    - goods / accessory: `> 3,780,000` (unchanged)
>    - **weapon: `> 99,999,999`** (changed)
>    - armor: `> 99,003,000` (unchanged)
>
> 3. **Decode fields (confirmed present on ER `EquipParamGoods`):**
>    - AP location id = `vagrantItemLotId` (low32) + `vagrantBonusEneDropItemLotId` (high32)
>    - local replacement real id = `basicPrice`
>    - local replacement qty = **`sellValue` vs `saleValue` — UNRESOLVED.** The spec names it both
>      ways; they're different columns. We must pin **one**. Tell me which the client reads and
>      I'll bake into that field. (The spec-2 harness dumps both so we can verify the actual bake.)
>    - foreign-remove flag = `disableUseAtOutOfColiseum` (item 1)
>
> 4. **Synthetic template good = id 8010** (inert key item: `goodsType=1`, `refId_default=-1`,
>    `isConsume=0`, `isEquip=0`, empty name). Synthetic rows are clones of it with the fields above
>    overwritten — FYI so the client treats them as placeholders, not real consumables.
>
> 5. **Decision A — connection string `EldenRing`** (exact, no space; same as the player's YAML
>    `game:` key — the original spec's repro step typo'd it as `Elden Ring` with a space). **Decision
>    B — `versions`** is enforced by the **static randomizer** at bake time, not the client, so the
>    client needs to do nothing with it (correction from the earlier framing).

---

## Quick checklist — status after the spec-2↔spec-3 reconciliation

- [x] **C resolved** → `disableUseAtOutOfColiseum` (78 vs 0, shared dump). spec-3 switches off `disableUseAtColiseum`.
- [x] **F (goods-only) confirmed** against the dump — goods is the only table with the full encoding surface. Randomizer bake already goods-only.
- [x] **D collapsed** by F → single goods threshold `> 3,780,000`; weapon/armor thresholds dead.
- [x] **B reconciled** → dual enforcement (randomizer @ bake + client @ connect), contract-version, lockstep now.
- [x] **qty field RESOLVED** → `sellValue` (paired with `basicPrice`); spec-3's call, randomizer matches.
- [x] **Goods-only gate answered** → goods placeable at all location types (`lotItemCategory=1`, `equipType=3`, grants via lots).
- [x] **`versions` check pinned** → dual-enforce, contract-version, lockstep, pre-release-aware; both checkers share spec-3's acceptance vector (no split-brain).
- [ ] Randomizer bake change: **force category=goods at the location** (override DS3 category-matching for ER).
- [ ] Coliseum field uses the **ER paramdef spelling** `disableUseAtOutOfColiseum` (not DS3's lowercase-f).
- [ ] Randomizer fork greps: bake `versions` check (reuse if node-semver-compatible), `SlotKey`/`Forced`/slot-enumeration; pull apworld `dfaf9a8`+.
- [ ] **`StableKey` byte-diff — the only build-gated item left.** Bake the two samples; the int64-encoding half is pre-de-risked by `vagrant_codec.py` (signed `s32` → unsigned recombine).
- [x] A–F **locked** (per spec-3's close-out) — any later drift is a silent interop break; bump the contract version.
