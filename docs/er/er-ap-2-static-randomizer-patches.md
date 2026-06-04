# ER AP Static-Randomizer — resolved values + drafted patches

*Drafted against the spec (spec 2 of 3). I have the vanilla ER params dump but **not** the
fork source, so concrete fork internals (method signatures, exact body of lines 330–403) are
marked `// VERIFY/PORT` — reconcile those against `4laric/SoulsRandomizers @ archipelago`.*

---

## 0. ER values resolved from the params dump

Dump is a **DLC-era full regulation export** (goods 2326, weapon 3554, protector 820,
accessory 157; weapon base ids span 100,000–99,060,000). That's the right oracle: the baked
`regulation.bin` carries the whole id space regardless of whether a base-game seed *places*
those rows, so thresholds must clear the full space.

### Synthetic base good (step 1) — **8010 confirmed**
| field | value | meaning |
|---|---|---|
| `Name` | *(empty)* | unused row — no display collision |
| `goodsType` | `1` | key item (persists in inventory) |
| `refId_default` | `-1` | inert — references nothing |
| `isConsume` | `0` | not consumed |
| `isEquip` | `0` | not equippable |
| `behaviorId` | `0` | no behavior |

8010 is a clean inert key-item template. Use it.

### Decision D — **resolved via goods-only routing; one live threshold**

> **Update (spec-2↔spec-3 reconciliation):** spec-3 established — and the dump confirms — that
> **all synthetic placeholders must be `EquipParamGoods` rows** (see "Goods-only" below). Under
> goods-only the client detects synthetics by the **goods threshold alone (`> 3,780,000`)**; the
> weapon and armor thresholds are **dead** — no synthetic ever lives in those tables, so a real
> weapon at 68.5M is just a real weapon and never misclassified. The weapon-threshold bug below is
> therefore **moot in the live design**; it's preserved as the answer *if* synthetic weapons were
> ever reintroduced (they can't be — the field table forbids it).

**Live value:** synthetic goods get ids `> 3,780,000` (real goods top out at 2,220,010; ~3,705
synthetic goods land ~3,780,001–3,783,705, clear of the 999,999,999 reserved row). That single
threshold is the whole of Decision D under goods-only.

<details><summary><b>Fallback analysis — weapon threshold, only relevant if synthetics were ever non-goods</b></summary>

The intent was "threshold sits above the vanilla max of that category." If synthetic weapons
existed, the original `weapon > 23,010,000` would be **broken**:

| category | real vanilla max (dump) | max in loot | original threshold | verdict |
|---|---|---|---|---|
| goods | 2,220,010 | — | `>3,780,000` | ✅ |
| accessory | 204,000 | — | `>3,780,000` | ✅ |
| weapon | **99,060,000** (base) | **68,510,000** | `>23,010,000` | ❌ broken |
| armor (protector) | 5,330,000 (base) | 5,060,300 | `>99,003,000` | ✅ |

666 base weapons exist (multiples of 10,000); class is encoded in the high digits, so real
droppable weapons run to 30M–68M (and 90M/99M dummies). `ItemLotParam_map`+`enemy` carry **174
distinct weapon drops above 23,010,000**, looted to 68,510,000 — independently corroborated by
spec-1's item table (**213 of 508 obtainable weapons above 23.01M, same 68.51M max**). The correct
value would be **`> 99,999,999`** (clears the 99,060,000 base max and the 99,999,999 sentinel).
Swapping weapon↔armor doesn't fix it (99,060,000 > 99,003,000). **But goods-only makes all of this
inert.**
</details>

### Goods-only synthetic routing — **forced; the dump shows why**

Every synthetic placeholder is a clone of the goods template (8010), and it **must** be — goods is
the only param table carrying the full encoding surface. Field availability across the four equip
tables:

| field (encoding role) | GOODS | WEAPON | PROTECTOR | ACCESSORY |
|---|:---:|:---:|:---:|:---:|
| `vagrantItemLotId` (loc id low32) | ✅ | — | — | ✅ |
| `vagrantBonusEneDropItemLotId` (loc id high32) | ✅ | — | — | ✅ |
| `basicPrice` (local replacement id) | ✅ | — | ✅ | ✅ |
| `disableUseAtOutOfColiseum` (Decision C flag) | ✅ | — | — | — |

Weapon lacks the vagrant pair **and** `basicPrice`; protector lacks the vagrant pair. Accessory is
the near-miss — it has the location-id and replacement fields, but **not** the Decision-C flag, so
it can't carry a *foreign* synthetic. **Goods is the only table with all four**, so route 100% of
synthetics through goods.

spec-3 sharpened *why* this is forced rather than merely preferred: even improvising a carrier on
weapon/protector fails — of weapon's all-zero fields, essentially only `wanderingEquipId` is a clean
u32 (the rest are bools, floats, or live combat stats), and the location-id + real-id encoding needs
**2–3** u32 carriers plus `basicPrice`. So "keep synthetic weapons/armor" would mean repurposing
game-read float/combat fields — a non-starter. Goods-only it is.

**Gate from spec-3 — can the randomizer emit a goods placeholder at *every* location type, not just
lot/shop? Yes.** Goods is a first-class category in each placement surface (confirmed against the
dump):

| location surface | category field | goods code | vanilla goods entries |
|---|---|---|---|
| item-lot slots (`ItemLotParam_map`/`enemy`) | `lotItemCategoryNN` | **1** | 4,031 |
| shop / NPC slots (`ShopLineupParam`, `_Recipe`) | `equipType` | **3** | 498 (+148 recipes) |
| event/script grants | (route through `ItemLotParam` lots) | **1** | — |

So a synthetic goods placeholder drops into any location by writing the **goods category code +
synthetic good id**. Event/script grants in ER overwhelmingly award an *item-lot id*, so retargeting
a grant = retargeting its backing lot entry to `(category 1, synthetic)` — already covered by the lot
path.

**The actual bake change (confirmed required, not just a `// VERIFY`).** spec-3 confirms DS3 places
synthetics **category-matched** (a synthetic at a weapon location is a synthetic *weapon* — which is
why the client even has `GetWeapon/Protector/AccessoryParam`). Porting that to ER unchanged would
emit weapon/armor synthetics that physically can't carry the encoding. So the ER placement layer
**must force category = goods at every location**, overriding the inherited category-matching — i.e.
write `lotItemCategory = 1` / `equipType = 3` (not the vanilla item's category) alongside the
synthetic good id. The client *simplifies* in return: one detection path, weapon mask and
weapon/armor thresholds dropped.

> `// VERIFY` (now a known change, not a maybe): find where the inherited placement chooses the
> synthetic's category from the vanilla item's type, and force it to goods for `FromGame.ER`.
> `AddSyntheticItem` (§1) is already goods-only for the *template*; this is about the *category
> written at the location*.

### Decision C — repurposed foreign-remove bool — **`disableUseAtOutOfColiseum`** (resolved)

**The spec's premise is wrong, and spec-3 has switched to this field.** ER's `EquipParamGoods` has
**both** Coliseum fields — and the counts (re-confirmed on the shared dump by *both* chats, 2,326
goods rows) decide it:

- `disableUseAtColiseum` (col 55) — **78 vanilla rows set** (would false-positive 78 real items)
- `disableUseAtOutOfColiseum` (col 56) — **0 set, all-zero** ← use this

A foreign-remove flag must be zero on every vanilla goods row or the client mis-reads real items as
foreign-removable. **Resolution: both use `disableUseAtOutOfColiseum`.**

> **Struct-regen gotcha (spec-3).** ER's paramdef label is exactly `disableUseAtOutOfColiseum`
> (capital **O**, capital **F** — "OutOf"), confirmed in the dump header. The DS3 struct's
> `disableUseAtOutofColiseum` (lowercase **f** — "Outof") is a *different label*. Both sides must
> reference the **ER paramdef name** so the randomizer sets and the client reads the **same bit** —
> a regenerated-from-DS3 struct with the lowercase-f spelling would silently mask a different field.
> (My §3 `SetGoodsBool(synth, "disableUseAtOutOfColiseum", true)` uses the ER spelling.)

Other all-zero booleans exist (`isFixItem`, `isBonfireWarpItem`, `isUseMultiPenaltyOnly`,
`isSleepCollectionItem`, `unknown_0x73_1`) but the Coliseum-family one keeps the client diff
smallest. Template 8010 has it `= 0`, so setting `1` on foreign goods cleanly encodes the flag.

**Do NOT** use `basicPrice` as the flag — it's reserved for the local-replacement real id, and its
all-zero-in-vanilla status is load-bearing: spec-3 uses `basicPrice = 0` as the clean "no local
item" default on foreign checks.

### qty field — **resolved → `sellValue`** (spec-3's call)

The local-replacement pair is **`basicPrice` (real id) + `sellValue` (qty)**, as inherited from DS3
— spec-3 picked `sellValue` over `saleValue` for parity. The vanilla distribution doesn't decide it
(synthetics are new rows the randomizer writes, and the client reads qty only on rows already
classified synthetic), so `sellValue`'s vanilla use is irrelevant to correctness. `saleValue` is the
fallback *only* if the game turns out to mutate `sellValue` on an in-inventory synthetic before the
swap — no evidence it does. **The randomizer writes `sellValue`** to match the client's decode.

### Foreign-item icon (step 3) — cosmetic, still `// VERIFY`
8010 uses `iconId 229`. DS3/SDT bake **custom** AP icons (6020/579) into the menu atlas;
cleanest is to do the same for ER and use that id. Fallback: any existing generic ER goods
icon. Left as `// VERIFY` since it depends on what you bake.


---

## 1. `AddSyntheticItem` — ER base good
`RandomizerCommon/PermutationWriter.cs` (~2122)

```csharp
new ItemKey(
    ItemType.GOOD,
    game.Type switch
    {
        FromGame.DS3 => 2005, // Small Doll
        FromGame.SDT => 2501, // Shelter Stone
        FromGame.ER  => 8010, // inert key item: goodsType=1, refId_default=-1, isConsume=0 (confirmed)
        var g => throw UnsupportedGame(g),
    }
),
```

---

## 2. `ConvertRandomizerOptions` — ER case
`RandomizerCommon/ArchipelagoForm.cs` (~588, `switch (type)`)

```csharp
case FromGame.ER:
    // apworld owns item logic & placement; set only the static knobs the ER engine reads.
    // AP Toggle/DefaultOnToggle serialize as int 0/1 (NOT JSON true/false) -> coerce; never `== true`.
    opt["item"]  = true;
    opt["mats"]  = AsBool(archiOptions["material_rando"]); // DefaultOnToggle -> 1 unless player opts out
    opt["enemy"] = AsBool(archiOptions["enemy_rando"]);    // Toggle          -> 0 by default
    if (AsBool(archiOptions["enemy_rando"])) opt["scale"] = true;
    // DLC is a separate track; refuse a DLC seed rather than silently half-baking one.
    if (AsBool(archiOptions["enable_dlc"]))
        throw new Exception("enable_dlc=1, but this ER static build is base-game only (DLC is a separate track).");
    // racemode OFF (AP supplies placement).
    break;
```

```csharp
// Coerce an AP option to bool. AP toggles arrive as int 0/1, so a boxed 0/1 is NOT a C# bool
// (and `if (0)` / `if (1)` won't compile). VERIFY the fork's slot_data value type:
//   boxed long  -> Convert.ToInt64(v) works as below;
//   JToken/JValue -> use ((JToken)v).Value<long>() != 0 instead.
static bool AsBool(object v) => Convert.ToInt64(v) != 0;
```

> Confirmed by spec-1: both keys are in `slot_data["options"]` — but emitted as **int `0`/`1`**,
> not `true`/`false`, so the coercion above is mandatory. Defaults differ: `material_rando` is
> **on** by default (`DefaultOnToggle`), `enemy_rando` **off** (`Toggle`) — so a default base-game
> seed bakes with the material pass on and the enemy pass off (and `opt["scale"]` therefore unset).
> Flag that if the MVP expectation differs. The full options blob is 29 keys; you read these three.

---

## 3. `BuildArchipelagoPlacement` — extraction, **scout folded in + self-contained**
`RandomizerCommon/ArchipelagoForm.cs`

This is the structural shell with all three fixes applied:
1. **scout folded in** — the helper does its own server round-trip (no closed-over `scoutedLocations`);
2. **self-contained** — `apLocationsToScopes` / `FindMatchingSlotKey` rebuilt from `data`/`ann`
   inside the method (the spec's own VERIFY);
3. ER **foreign-icon** case + Decision-C **flag** wired.

The per-location loop body is where your fork's lines **330–403** go almost verbatim — I've
marked the region and kept the branch structure the spec describes, but the exact statements
depend on fork-internal APIs I can't see (`AddSyntheticCopy`, `FindMatchingSlotKey`,
`SyntheticItemName`, `AddMulti`, `replaceWithInArchipelago`). **Port your real body into the
marked block**; the value here is the method shell, not a line-for-line reproduction of code I
don't have.

```csharp
// Returns (forced placement, removals) for perm.Forced(...).
// Self-contained: performs its own scout and rebuilds all per-location maps from data/ann.
public static (Dictionary<SlotKey, List<SlotKey>> items,
              Dictionary<SlotKey, List<SlotKey>> remove)
    BuildArchipelagoPlacement(
        FromGame type, ArchipelagoSession session, GameData game,
        LocationData data, AnnotationData ann, PermutationWriter writer,
        IReadOnlyDictionary<long, int>    apIdsToItemIds,
        IReadOnlyDictionary<long, uint>   itemCounts,
        IReadOnlyDictionary<long, string> locationIdsToKeys)  // <-- REQUIRED to resolve locations (was missing)
{
    var items         = new Dictionary<SlotKey, List<SlotKey>>();
    var itemsToRemove = new Dictionary<SlotKey, List<SlotKey>>();

    // --- (a) FOLDED-IN SCOUT: one server round-trip, awaited synchronously to fit
    //         Randomize's synchronous flow (the DS3 path scouts the same way). -----------
    long[] locationIds = session.Locations.AllLocations.ToArray(); // VERIFY: AllLocations (all defined),
                                                                    // not AllMissingLocations — baking needs every slot
    Dictionary<long, ScoutedItemInfo> scoutedLocations =
        session.Locations
               .ScoutLocationsAsync(HintCreationPolicy.None, locationIds) // VERIFY: overload/return shape
               .GetAwaiter().GetResult();                                  // for THIS Archipelago.MultiClient.Net version

    // --- (b) SELF-CONTAINED RESOLUTION (full treatment in §3.1). Build a reverse index ONCE
    //         from the slots the permutation actually knows, so every lookup returns a CANONICAL
    //         SlotKey instance by construction (this is the §6 correctness check, made structural).
    var keyToSlot = new Dictionary<string, SlotKey>();
    foreach (SlotKey sk in EnumerateKnownSlots(data, ann))   // VERIFY: how the fork enumerates slots
        keyToSlot[StableKey(sk)] = sk;                       // VERIFY: StableKey MUST equal the apworld's
                                                             //         locationIdsToKeys value (the 1:1 contract)

    SlotKey FindMatchingSlotKey(ScoutedItemInfo info)
    {
        if (!locationIdsToKeys.TryGetValue(info.LocationId, out string keyStr)) return null; // 1:1: should never miss
        return keyToSlot.TryGetValue(keyStr, out var sk) ? sk : null;  // canonical by construction
    }

    // 1:1 contract guard — fail LOUD before baking a half-resolved regulation (see §3.1).
    VerifyApResolution(scoutedLocations, locationIdsToKeys, keyToSlot);

    int mySlot = session.ConnectionInfo.Slot;

    // ===================== PORT: original loop body, lines 330–403 =====================
    // DETERMINISM (see §6): iterate a SORTED location-id list and INDEX into every map —
    // never enumerate Dictionary/scout order. Sorted iteration pins (1) the AddSyntheticCopy
    // id counter and (2) Forced/Write insertion order, so the bake is byte-reproducible
    // regardless of .NET Dictionary order, SlotKey hash order, or AP-lib scout order.
    foreach (long apLocId in scoutedLocations.Keys.OrderBy(id => id))
    {
        ScoutedItemInfo info = scoutedLocations[apLocId];   // index, don't enumerate

        SlotKey here = FindMatchingSlotKey(info);
        if (here == null) continue;                      // VERIFY: original's skip condition

        bool foreign = info.Player != mySlot;

        if (foreign)
        {
            // Foreign: synthetic placeholder that the runtime client resolves over the AP protocol.
            SlotKey synth = writer.AddSyntheticCopy(      // VERIFY: real method name/return
                /* template */ /* the 8010-based good */,
                name:   SyntheticItemName(info),          // "[Player]'s X"
                iconId: type switch {
                    FromGame.DS3 => 6020,
                    FromGame.SDT => 579,
                    FromGame.ER  => /* VERIFY: custom AP icon id in the ER menu atlas, else a generic ER icon */ 229,
                    var g => throw UnsupportedGame(g),
                },
                apLocationId: info.LocationId);           // encoded into Vagrant{Item,BonusEneDrop}LotId by AddSyntheticCopy

            // --- Decision C: foreign-remove flag ---------------------------------------
            // Set on the synthetic FOREIGN good only; the runtime client (spec 3) reads the
            // SAME field. ER uses disableUseAtOutOfColiseum (all-zero in vanilla; 8010 = 0).
            // VERIFY: set inside AddSyntheticCopy, or via the row handle it exposes.
            writer.SetGoodsBool(synth, "disableUseAtOutOfColiseum", true);  // VERIFY API

            AddMulti(items, here, synth);                 // VERIFY: original accumulation
        }
        else
        {
            // Local: resolve the real ER item by INDEXING the maps (never enumerate) —
            // valid here because info.Player == mySlot, so info.ItemId is in THIS slot's id space:
            //   int  erItemId = apIdsToItemIds[info.ItemId];
            //   uint qty      = itemCounts.TryGetValue(info.ItemId, out var c) ? c : 1u;
            // (Foreign items skip this — their code lives in another world's space and is NOT
            //  in apIdsToItemIds; the placeholder is named from info.ItemName instead.)
            // shop/crow get the real item copied; everything else gets a placeholder
            // tagged replaceWithInArchipelago that the client swaps to the real item on pickup.
            // (game-agnostic per the spec — transfers as-is)
            // PORT: original local branch verbatim.
        }
        // PORT: any itemsToRemove population from the original loop.
    }
    // ================================ end PORT =========================================

    // Path of the Dragon branch is `type == FromGame.DS3`-gated → inert for ER, no change.

    return (items, itemsToRemove);
}
```

> **Design note (the scout):** folding the scout in (per your ask) makes the helper block on
> the network. The cleaner-but-bigger alternative is to keep the scout in the caller and pass
> `scoutedLocations` in — then the helper is pure and trivially testable. The shell above
> isolates the scout as step (a) so you can hoist it later without touching the loop.

### 3.1 `FindMatchingSlotKey` — AP location id → **canonical** randomizer `SlotKey`

This is the densest `PORT` region, and it's where the §6 reference-equality trap and the
spec's 1:1 location contract both live. The resolution is a three-hop lookup:

```
AP location id ──(locationIdsToKeys, from slot_data)──▶ key string
key string ──(must match the randomizer's own slot identity)──▶ canonical SlotKey in data/ann
```

`locationIdsToKeys` is the apworld's authoritative map; the randomizer's only job is to turn
each key string into the **same `SlotKey` instance the permutation already built its graph from**
— not a freshly-constructed one.

**Why canonical-vs-constructed is the whole ballgame.** `Forced`/`Write` look slots up in
`Permutation`'s internal dictionaries. If `SlotKey` uses reference equality (or `Forced` indexes
by reference), a `new SlotKey(item, scope)` with identical *contents* is a different object →
the lookup misses → **the forced placement silently no-ops** (location keeps its vanilla item, no
error). If `SlotKey` has value equality you may survive, but you're betting the bake on an
equality override you haven't verified. Make it structural instead:

```csharp
// WRONG — fresh instance; misses every reference-keyed lookup → Forced does nothing, silently.
return new SlotKey(itemKey, scope);                         // ❌

// RIGHT — return the instance already registered in data/ann.
return keyToSlot[keyStr];                                   // ✅ (reverse index, §3 step (b))
```

The reverse-index pattern from §3 step (b) makes "canonical" unforgeable: every value in
`keyToSlot` came out of `EnumerateKnownSlots(data, ann)`, so anything `FindMatchingSlotKey`
returns is reference-identical to what the permutation uses. That converts a discipline you might
forget into an invariant the type system enforces.

**The real contract hiding here — now PINNED by spec-1.** `StableKey(slot)` must produce the
*exact* string the apworld wrote into `locationIdsToKeys`. Spec-1 confirmed the grammar (and that
the literals are hardcoded from nex3's ER slot definitions — the **same lineage** as your fork, so
they should match by construction):

```text
world pickup (item lot / event):  "{mapId:D6},0:{lotId:D10}::"            e.g.  180000,0:0018007000::
shop / NPC exchange:              "{mapId:D6},0:0000000000:{shopId:D6}:"  e.g.  111000,0:0000000000:101898:
```

`mapId` is `mAA_BB_CC` rendered `AABBCC` (e.g. `180000` = m18_00_00); the `0` block is a DLC
reserve; the trailing field is always empty. **Prefer returning the slot's *native* identity
string** — the one nex3's randomizer already assigns — rather than re-formatting from parts;
`StableKey` should be a thin accessor, not a re-implementation of the scheme. **Verification (once
both sides build):** dump `StableKey` for one world-pickup slot and one shop slot and confirm
byte-identical to the two literals above. Match ⇒ Decision E is closed.

**Shops: many AP locations → one slot (expected, and already handled).** Spec-1 confirmed
`locationIdsToKeys` values are **not unique** — 3,705 values, 3,100 distinct, **605 collisions
across 222 shared keys covering 827 locations**. Every collision is a shop/exchange slot backing
many AP checks (Enia's remembrance exchange = 30; Twin Maiden Husks = 21; Kalé = 14; …). This is
the inherited DS3 shop model, and the design above handles it for free — *provided* two things stay
straight:
- `keyToSlot` is **`key → SlotKey`**, never `key → apId`. The "reverse index" term above means
  *string-key → canonical-slot*; do **not** build the other reverse map (`key → apId`), which would
  collapse a whole shop inventory into one location. `FindMatchingSlotKey` resolves **forward, per
  `apId`**, so 30 distinct Enia `apId`s all resolve to the one Enia slot — correctly.
- The §3 loop runs **once per `apId`** (it iterates all of `scoutedLocations.Keys`), so each of the
  30 Enia checks gets its own synthetic placeholder, and `AddMulti` **accumulates** them into
  `items[eniaSlot]`'s list. A shop slot legitimately ends up with N forced entries — exactly what
  the writer's shop-multi handling expects.

So N:1 needs no new code — but it's *why* the value in `Dictionary<SlotKey, List<SlotKey>>` is a
**list**, and why `AddMulti` must append, not overwrite. (Confirm `Forced`/`Write` place a slot's
item-list as N separate shop entries for ER — inherited DS3 behavior, should transfer.)

**Fail loud, not silent — the guard referenced in §3:**

```csharp
static void VerifyApResolution(
    Dictionary<long, ScoutedItemInfo> scouted,
    IReadOnlyDictionary<long, string> locationIdsToKeys,
    Dictionary<string, SlotKey> keyToSlot)
{
    int missingKey = 0, unknownSlot = 0, ok = 0;
    foreach (long apId in scouted.Keys)
    {
        if (!locationIdsToKeys.TryGetValue(apId, out string ks)) { missingKey++; continue; }
        if (!keyToSlot.ContainsKey(ks)) unknownSlot++;            // StableKey vs apworld scheme mismatch
        else ok++;
    }
    if (missingKey > 0 || unknownSlot > 0)
        throw new Exception(
            $"AP location resolution broken: {missingKey} scouted locations absent from " +
            $"locationIdsToKeys, {unknownSlot} key-strings not found among randomizer slots " +
            $"(StableKey/locationIdsToKeys scheme mismatch?). Resolved {ok}/{scouted.Count}. " +
            "Refusing to bake a half-resolved regulation.");
}
```

Without this, a scheme mismatch produces a regulation that *looks* fine and silently leaves most
locations vanilla — the worst kind of bug to debug post-bake. The guard tells you immediately which
side is wrong and by how much. (Spec-1 already hit a related case: one location's AP *id* — the
dict **key** in `locationIdsToKeys`, not the `StableKey` value — was emitted as the string
`'Golden Rune [5]'` instead of an int; its real int id would have missed the map, firing exactly
this guard. Fixed apworld-side — pull `dfaf9a8`+. In C# it would likely fail even earlier, at
slot_data parse, when a non-numeric key won't deserialize into `Dictionary<long, …>`.)

**Three things to confirm in the fork** (I'm reconstructing the types; these resolve the `VERIFY`s):

```bash
grep -rn "class SlotKey\|struct SlotKey" RandomizerCommon/          # definition + fields
grep -rn "Equals\|GetHashCode" RandomizerCommon/<SlotKey file>      # value vs reference equality
grep -rn "public.*Forced(" RandomizerCommon/Permutation.cs          # confirm items/remove direction
```

- **`SlotKey` shape.** Likely `(ItemKey Item, ItemScope Scope)` in RandomizerCommon — but verify,
  because it dictates both `StableKey` and how `EnumerateKnownSlots`/`keyToSlot` are built.
- **`Forced` direction.** Is `items` keyed by the *location* slot (vanilla item there) with the
  synthetic/real item as the value list, or the reverse? The reverse index and the loop's
  `AddMulti(items, here, synth)` assume *location → items*; flip if the fork's `Forced` expects
  the other orientation.
- **Slot enumeration + key fetch.** Replace `EnumerateKnownSlots(data, ann)` with the fork's
  actual accessor (e.g. iterating `data.Locations` / `ann.Slots`). The synthetic/real-item **values**
  should likewise come from `AddSyntheticCopy`'s return (canonical by construction) or be fetched
  from `data` for shop/crow reals — never `new`'d.

---

### 3.2 Location-id encoding — the vagrant fields are **signed**; recombine **unsigned**

`AddSyntheticCopy` writes the AP location id (int64) split across two fields — `vagrantItemLotId`
(low 32) and `vagrantBonusEneDropItemLotId` (high 32) — and the client reads it back. The dump
settles a subtlety that's otherwise a silent corruption: **both fields are signed `s32`** in ER's
paramdef (vanilla fills them with `-1`, which a `u32` could never hold). So a 32-bit half with bit 31
set reads back as a **negative** C# int, and a naive `(long)low | ((long)high << 32)` sign-extends
the low half and corrupts the id.

This is not a contrived edge: AP allocates location ids as a large base + index, so a realistic id
(e.g. `11,000,003,704`) has bit 31 set in its low half — the naive recombine turns it into
`-1,884,898,184`. The recombine **must** treat each half as unsigned:

```csharp
// ENCODE (bake): int64 -> two s32 fields (same 32 bits; the low half may store as negative)
int low  = unchecked((int)(apId & 0xFFFFFFFFL));
int high = unchecked((int)((apId >> 32) & 0xFFFFFFFFL));
// DECODE (client): UNSIGNED recombine — the (uint) casts prevent sign extension
long apId = ((long)(uint)low) | (((long)(uint)high) << 32);
```

`basicPrice` (real id) and `sellValue` (qty) carry only positive values well under 2³¹, so their
signedness is harmless — the hazard is specific to the int64 split. The golden codec + round-trip
test vectors live in **`vagrant_codec.py`** (presented alongside); it's the Decision-E byte-diff's
shared oracle — both sides port the two snippets above and check against it.

---

## 4. `Randomizer.Randomize` — AP plumbing + **writer-scope fix**
`RandomizerCommon/Randomizer.cs`

### 4a. Carry AP inputs (signature ~48)
```csharp
public sealed class ArchipelagoData
{
    public ArchipelagoSession Session;
    public IReadOnlyDictionary<long, int>    ApIdsToItemIds;
    public IReadOnlyDictionary<long, uint>   ItemCounts;
    public IReadOnlyDictionary<long, string> LocationIdsToKeys;   // <-- needed by FindMatchingSlotKey (§3.1)
}

public void Randomize(
    RandomizerOptions opt, FromGame type, Action<string> notify = null,
    string outPath = null, Preset preset = null, Messages messages = null,
    bool encrypted = true, string gameExe = null, MergedMods modDirs = null,
    ArchipelagoData ap = null)              // <-- new
```

### 4b. ER `if (opt["item"])` block (~311–358) — reorder that **preserves the non-AP path**

The fix for the hazard I flagged: **declare** `PermutationWriter writer;` in the shared scope
but **defer construction**, so the `else` branch keeps vanilla's *Logic-then-construct*
ordering exactly. If the ctor touches `game`/`data`/`ann`, hoisting it for *both* branches
would desync the non-AP build and break your step-3 oracle diff for reasons unrelated to AP.

```csharp
Permutation perm = new Permutation(game, data, ann, messages, explain: opt["explain"]);

// Declared here for the shared Write(), but construction is DEFERRED per-branch:
// the non-AP path must construct the writer AFTER Logic() (vanilla ordering) so the
// oracle diff stays byte-identical.
PermutationWriter writer;

if (ap != null)
{
    // AP path: writer must exist BEFORE placement so synthetic items can be built via it.
    writer = new PermutationWriter(game, data, ann, null, itemEventConfig, messages, coord);

    var (items, remove) = ArchipelagoForm.BuildArchipelagoPlacement(
        type, ap.Session, game, data, ann, writer,
        ap.ApIdsToItemIds, ap.ItemCounts, ap.LocationIdsToKeys);

    perm.Forced(items, remove);                       // Forced consumes no RNG -> determinism intact
    perm.Logic(new Random(seed), opt, null, new List<Permutation.RandomSilo> {
        Permutation.RandomSilo.INFINITE,      Permutation.RandomSilo.INFINITE_SHOP,
        Permutation.RandomSilo.INFINITE_GEAR, Permutation.RandomSilo.INFINITE_CERTAIN,
        Permutation.RandomSilo.MIXED,
    });
}
else
{
    // Non-AP: IDENTICAL to vanilla — Logic() first, THEN construct the writer.
    perm.Logic(new Random(seed), opt, preset);
    writer = new PermutationWriter(game, data, ann, null, itemEventConfig, messages, coord);
}

notify?.Invoke(messages.Get(editPhase));
permResult = writer.Write(new Random(seed + 1), perm, opt);   // seed/seed+1 split preserved
```

> Determinism: `seed`/`seed+1` split preserved; `Forced` takes no `Random` so it can't desync
> the AP branch. **But** the infinite-silo list here is the DS3 partition — verify it's
> *exhaustive* for ER (§6) or some ER silo is left unplaced. Full reproducibility + correctness
> analysis (iteration order, static RNG, the silo partition, the three-test oracle) is in §6–§7.

---

## 5. `ArchipelagoForm` dispatch — route ER to `Randomize`
`RandomizerCommon/ArchipelagoForm.cs`

```csharp
// Icon (~68):
FromGame.ER => "$this.ERIcon",            // add an ERIcon resource

// Login name (~173) — MUST equal the apworld's game = "EldenRing" exactly, no space:
FromGame.ER => "EldenRing",
```

```csharp
// Short-circuit ER BEFORE the DS3/SDT hand-rolled data block (~252):
if (type == FromGame.ER)
{
    new Randomizer().Randomize(
        opt, FromGame.ER, status => SetStatusText(status),
        messages: /* messages */, gameExe: /* exe path */, modDirs: /* MergedMods */,  // VERIFY plumbing
        ap: new ArchipelagoData {
            Session           = session,
            ApIdsToItemIds    = apIdsToItemIds,
            ItemCounts        = itemCounts,
            LocationIdsToKeys = locationIdsToKeys,   // VERIFY: decoded from slot_data alongside the others
        });
    WriteConfigFiles(slotData);
    return;
}
// ... existing DS3/SDT path unchanged ...
```

> `// VERIFY`: ER's `Randomize` needs `gameExe`/`modDirs` for the UXM-vs-ModEngine write path.
> Decide how the AP client supplies these (config field / fixed). The DS3/SDT branch can later
> be refactored to call `BuildArchipelagoPlacement` too, killing the duplication — optional.

### 5.1 `versions` — dual enforcement, contract-version, lockstep for now (Decision B, reconciled)

Decision B went through two corrections. (1) spec-1 corrected our original "client enforces" → the
**static randomizer** enforces at bake (inherited DS3). (2) spec-3 then upgraded that to a **union**:
the randomizer checks at bake **and** the client checks at connect, because a validated bake doesn't
prove the *connecting* client shares the contract — that mixed-version hole is real and the client
check closes it cheaply. And `versions` is reframed as the **encoding-contract version** (a single
shared value), not this binary's own semver.

So: **apworld emits ↔ {randomizer @ bake, client @ connect} enforce**, value = contract version.

```bash
grep -rn "versions" RandomizerCommon/ArchipelagoForm.cs RandomizerCommon/Randomizer.cs   # find the bake check
```

- **Confirm the bake check runs for ER** (unconditional, or add `FromGame.ER` if gated to DS3/SDT).
- **Give the randomizer the *contract* version it implements** to check against — not its binary
  version. A contract change (any A–F shift, incl. a synthetic-field change) bumps it.
- **Range form — lockstep now.** I'd argued for bounded `">=0.1.0 <0.2.0"`, but that read assumed
  the contract was nearly frozen — it isn't: **goods-only (Decision D collapse) is a pending change
  I hadn't seen**, and the qty pick + StableKey diff are still open. So adopt spec-3's per-build
  lockstep `">=0.1.0-beta.N <0.1.0-beta.(N+1)"` until those land, then **freeze and graduate** to
  bounded `">=0.1.0 <0.2.0"`. Nothing has shipped, so the corrected C/D/E + goods-only **are** the
  baseline — that baseline is `beta.1`; graduating to a released `0.1.0` is the freeze point.
  (apworld currently emits `">=0.1.0-beta.1 <0.1.0-beta.2"`.) Semver note: plain ranges exclude
  pre-releases, so the lower bound must itself be the pre-release.

**The bake check must be pre-release-aware too — and share the client's exact semantics.** spec-3
confirmed its connect-time check is pre-release-aware (it would otherwise reject the valid
`0.1.0-beta.1` the apworld emits during lockstep). The **same applies to this binary's bake check**,
and there's a sharper reason than symmetry: if the randomizer's `satisfies` and the client's disagree,
a slot_data can pass at bake and be **rejected at connect** (or vice versa) — a version split-brain.
So both checkers must implement identical semantics and pass one shared vector (spec-3's, node-semver):

| contract ver | lockstep `">=0.1.0-beta.1 <0.1.0-beta.2"` | graduated `">=0.1.0 <0.2.0"` |
|---|---|---|
| `0.1.0-beta.1` | **PASS** | reject |
| `0.1.0` | reject | **PASS** |
| `0.2.0-beta.1` | reject | reject *(no `includePrerelease`)* |
| `0.2.0` | reject | reject |

So this binary's contract-version constant is `0.1.0-beta.1` during lockstep (it must PASS the lockstep
range at bake), graduating to `0.1.0` at freeze — in lockstep with the apworld range and the client's
own version. **Likely free:** DS3's own range is pre-release-bounded (`3.0.0-beta.24`), so nex3's
inherited bake check almost certainly already handles pre-releases correctly — confirm its semantics
match the vector and reuse it. If you must write it fresh, use a **node-semver-compatible** satisfies
(e.g. the `SemanticVersioning` NuGet, a node-semver port) — **not** `NuGet.Versioning`, whose
pre-release rules differ (the .NET analogue of spec-3's "not PEP 440" warning). No blanket
`includePrerelease` (it would leak a future-breaking `0.2.0-beta.1` through the graduated range).

---

## 6. Determinism — what's free, what you must pin, what's a correctness trap

Three different properties get conflated under "determinism." Only one is at risk.

**Functional determinism (free).** Every player gets the right item at every location. Guaranteed
by `Forced` placing exactly the `slot_data` set, and it does **not** depend on iteration order or
on the synthetic row's id: the client keys each location off the `(low32, high32)` AP location id
baked into `vagrantItemLotId`/`vagrantBonusEneDropItemLotId`, reads the local replacement from
`basicPrice`/`sellValue`, and the foreign flag from `disableUseAtOutOfColiseum` — none of which is
the row id. Correctness survives any row-ordering or id-assignment shuffle. It was never on the
table.

**Byte-reproducibility (at risk → fixed in §3).** Same seed + same `slot_data` + same scout →
bit-identical `regulation.bin`. Needed for the test oracle and user trust. The threat is
**iteration order**, and it chains:

> scout-result order → your `foreach` insertion order → **(a)** the `AddSyntheticCopy` id counter
> (increments per row in creation order → the *same location* can get a *different* synthetic
> row-id across bakes) and **(b)** `Dictionary` enumeration in `Forced`/`Write`.

Every link is nondeterministic by .NET contract: `Dictionary<,>` enumeration order is officially
undefined (stable on CoreCLR only for a deletion-free dict, and a function of the key's
`GetHashCode` — if `SlotKey` uses reference hashing it's effectively random run-to-run), and the
AP library's scout return order isn't contractual either.

**Fix — one rule (applied in §3):** drive everything off a **sorted location-id list** and
**index** into the maps; never enumerate them. Sorted insertion pins synthetic row-ids; whether
`Write` serializes rows sorted-by-id (SoulsFormats' usual behavior) or in insertion order, both
collapse to deterministic once ids and insertion are. Same for `apIdsToItemIds`/`itemCounts` —
look up by key, don't iterate.

**Two residuals I can't settle without the source:**
- **Grep for an ambient `static Random` / `Random.Shared`** in `PermutationWriter` and the
  material/enemy passes. If one exists, the AP branch's different call sequence (Forced before a
  *shorter* Logic) shifts that stream and reintroduces nondeterminism through a side door, with no
  visible seed. Single most likely hidden leak.
- **Confirm `FindMatchingSlotKey` returns the canonical `SlotKey` from `data`/`ann`**, not a
  freshly-constructed one. If `SlotKey` is reference-equal, a rebuilt key won't match what
  `Forced`/`Write` expect — a *correctness* bug wearing a determinism costume.

**Correctness trap — the silo partition (relates to §4b).** The AP `Logic` rolls only
`{INFINITE, INFINITE_SHOP, INFINITE_GEAR, INFINITE_CERTAIN, MIXED}` — the DS3 partition. ER's
pipeline is richer. The invariant: **every `Permutation.RandomSilo` that vanilla ER `Logic` would
roll must be either covered by `Forced` or present in this list.** A silo that's neither forced nor
rolled is left empty → unplaced items, a writer that chokes, or nondeterministic fallback.
Enumerate `RandomSilo` for `FromGame.ER` and confirm the partition is exhaustive — the most likely
place the infinite-silo shortcut silently drops something ER-specific.

---

## Open / verify checklist (updated)
- [x] **Decision D resolved via goods-only** (§0): only the **goods** threshold `> 3,780,000` is live; weapon/armor thresholds dead. Weapon-threshold bug moot (kept as fallback).
- [x] **Decision C resolved → `disableUseAtOutOfColiseum`** (§0): authoritative counts 78 (disableUseAtColiseum) vs 0 — **spec-3 switches off `disableUseAtColiseum`**.
- [x] **Goods-only confirmed placeable everywhere** (§0): item lots `lotItemCategory=1`, shops `equipType=3`, event grants via lots. Gate answered: yes.
- [ ] **Force category=goods at the location** (§0/§1): DS3 placement is category-matched (confirmed) — for ER, write `lotItemCategory=1`/`equipType=3` (not the vanilla item's category) alongside the synthetic good id. This is the real bake change; find where the inherited placement picks the category from item type and override it for `FromGame.ER`.
- [x] **qty field resolved → `sellValue`** (§0): replacement pair = `basicPrice` (real id) + `sellValue` (qty). Randomizer writes `sellValue`.
- [ ] **Coliseum field = ER paramdef spelling `disableUseAtOutOfColiseum`** (capital O-F) — NOT the DS3 struct's `disableUseAtOutofColiseum`; match the ER paramdef so both sides mask the same bit.
- [ ] **Sorted-iteration discipline** in `BuildArchipelagoPlacement` (§3/§6) — sorted location-id loop, index never enumerate.
- [ ] **Grep for ambient `static Random`/`Random.Shared`** in `PermutationWriter` + material/enemy passes (§6) — highest-risk hidden nondeterminism leak.
- [ ] **`FindMatchingSlotKey` returns the canonical `SlotKey`** from `data`/`ann`, not a rebuilt one (§3.1/§6) — reverse-index pattern makes this structural; reference-keyed misses make `Forced` silently no-op.
- [ ] **Decision E byte-diff — two halves, both pending a bake.** (a) `StableKey` of one pickup + one shop slot byte-identical to the apworld literals (`180000,0:0018007000::`, `111000,0:0000000000:101898:`); format pinned (§3.1). (b) the **vagrant int64 encoding** round-trips — bake the two samples, decode `(vagrantItemLotId, vagrantBonusEneDropItemLotId)` per `vagrant_codec.py`, confirm the server's AP id. **Vagrant fields are signed `s32` (§3.2) → recombine unsigned** or bit-31 ids corrupt.
- [ ] **Shop N:1 handling** (§3.1): `keyToSlot` is key→slot (not key→apId); loop per-apId; `AddMulti` appends. No code change, but confirm `Forced`/`Write` emit a slot's item-list as N shop entries for ER.
- [ ] **`locationIdsToKeys` threaded** through `ArchipelagoData` → `Randomize` → `BuildArchipelagoPlacement` (§3/§4a/§5) — was missing from the original signature.
- [ ] Confirm in the fork (§3.1 greps): `SlotKey` definition + equality, `Forced` items/remove direction, the slot-enumeration/fetch accessor.
- [ ] **Silo partition exhaustive for ER** (§4b/§6) — every `RandomSilo` vanilla ER `Logic` rolls is forced or in the infinite list.
- [ ] Port the real 330–403 body + slot-enumeration into `BuildArchipelagoPlacement`; reconcile the scout overload/return shape for your AP-lib version.
- [ ] **Options 0/1 coercion + DLC refusal** in §2 (`AsBool`) — spec-1 confirmed toggles serialize as int `0`/`1`; `material_rando` defaults **on**, `enemy_rando` **off**. (Key presence: confirmed.)
- [ ] **`versions` — dual enforcement + contract-version + lockstep + pre-release-aware** (§5.1). Randomizer @ bake **and** client @ connect; bake-check constant `0.1.0-beta.1` during lockstep. **Both checkers must pass spec-3's shared acceptance vector** (node-semver semantics) or risk a bake-accept/connect-reject split-brain. Likely reuse nex3's inherited check (DS3 uses a pre-release-bounded range); if fresh, node-semver-compatible lib (`SemanticVersioning`), **not** `NuGet.Versioning`; no blanket `includePrerelease`. Confirm it runs for ER.
- [ ] **Pull apworld `dfaf9a8`+** — fixes a string-keyed `locationIdsToKeys` entry (`'Golden Rune [5]'`) that would have tripped `VerifyApResolution`.
- [ ] ER `Randomize` `gameExe`/`modDirs` plumbing; pick the foreign-item icon id.
- [ ] Run **Test 1 reproducibility gate** (`repro_diff.py`) once the build bakes — fastest signal the ordering fix holds.

## 7. Test plan — three tests, not one

"Diff against vanilla v0.11.4" is three tests wearing one trenchcoat. A raw byte-diff there is
meaningless: AP placements differ from a vanilla roll, the infinite silos differ (seed-stream
divergence), and mats/enemy differ when on — the diff is all *expected* noise. Split it.

**Pre-req (done):** apworld base-game seed, `enable_dlc=false`, 3,705 locations. Then point the
build at the AP server and confirm it connects (`"EldenRing"`), scouts, and bakes without throwing.

**Test 1 — Reproducibility gate.** AP-build(seed) vs AP-build(seed), *same* seed/slot_data/scout.
- Tier 1: `sha256sum` both `regulation.bin`. Identical → fully reproducible, stop.
- Tier 2: if the bins differ, run `repro_diff.py` (presented alongside) on CSV exports of both. A
  clean normalized diff ⇒ the raw difference is benign (row ordering / encryption nonce). Any
  row-level diff — especially a shifted synthetic id or a changed Vagrant/`basicPrice` field — is a
  real ordering or static-RNG leak (→ §6).

  This is where the §3 sorted-iteration fix proves itself.

**Test 2 — Writer-correctness oracle.** AP-build vs vanilla-build, **mats + enemy OFF on both**,
compared **by invariant, not equality**:
- every AP location's slot holds a valid row;
- all synthetic ids clear the goods threshold and collide with nothing real (confirmed clean: real
  goods stop at 2,220,010; synthetic land ~3,780,001–3,783,705; only the 999,999,999 reserved row
  sits above);
- every foreign synthetic carries the Decision-C bool (`disableUseAtOutOfColiseum=1`);
- every local non-shop placeholder carries `replaceWithInArchipelago`; shop/crow hold real items;
- placement-independent transforms (coordinator wiring, healthbar renames) match vanilla.

**Test 3 — Refactor regression.** Non-AP path of your **modified** binary vs the **unmodified
fork** binary → **byte-identical**. Exactly what the §4b writer-scope deferral guarantees.
- ⚠ Baseline is the **fork's own pre-AP build**, *not* the Nexus v0.11.4 release. Unless you've
  confirmed the fork's base tree *is* v0.11.4, those won't byte-match with zero changes and you'd
  be chasing a phantom.

**Spot-check (manual):** foreign location → synthetic placeholder (icon + `[Player]'s X`); local
non-shop → `[Placeholder] X` (client swaps); shop/crow → real items.

> `repro_diff.py` implements Test 1 Tier 2 and the structural half of Test 2 — normalized,
> ID-sorted, field-level CSV compare with a dedicated synthetic-goods decode (Vagrant ids,
> `basicPrice`, `sellValue`/`saleValue`, the Decision-C bool). Note the spec is internally
> inconsistent on `sellValue` (handoff) vs `saleValue` (project-context, col 92) for the qty
> field — the harness prints both so the actual encoding is visible regardless.
