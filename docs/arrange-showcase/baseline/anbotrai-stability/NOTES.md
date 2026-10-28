# anbotrai-stability baseline arrange sweep

Lock owner `baseline-stability`. Model restored and verified (`jq -S 'sort_by(.name)'` diff empty) after the portal Arrange and after every engine, then unlocked.

## Screenshots

- `01-mangled-*`: all cards in one column under the root.
- `02-portal-arrange-*`: portal Arrange then Save.
- `03-restored-designer`: after `restore`, column again.
- `04`..`08` = `auto`, `layered`, `tree`, `network`, `keep-order`, each `-designer` and `-graph`.

## Portal Arrange

- Needed Save. After clicking Arrange the cards moved but Save and Undo/Discard were enabled; after Save they greyed out.
- Positions only exist in the service after Save (export gave 17 positions only then).
- Result is a tree: root, `plan-trip`, six services in one row, resources below.

## Portal Arrange vs `auto` (jq on `layouts/anbotrai-stability.portal-arrange.json` and `.auto.json`)

- Canvas: both span x 0..2000. Max y 597 (portal) vs 576 (auto).
- Row pitch: portal 193 (rows y 0, 193, 386, 579..597), auto 192 (y 0, 192, 384, 576).
- Resource row y is ragged in portal (579, 589, 597 depending on card height), uniform 576 in auto.
- Order is mirrored: portal puts `preferences` at x=0 and `assistant` at x=1750; auto puts `assistant` at x=500 and `preferences` at x=2000.
- Root and `plan-trip`: portal x=625, auto x=1250 (root at the centre of the children).
- Portal root sits left of the children's centre (children span 0..2000, centre 1000); auto root x=1250 is also not at 1000.

## Engines

- `auto`, `layered`, `tree`, `network`, `keep-order` gave byte-identical positions (same md5 on `jq -S 'sort_by(.name)'`). On this model with a mangled start there is nothing to separate them.
- `auto` log: layered and tree both score 6 (0 edges through cards, 0 false links, 0 crossings, 0 label clashes); "keeping layered, the first of equal scores".
- All runs: 1 part, 2200 x 706 px, 17 of 17 positions change.

## Problems

- Designer page served a stale layout when navigated to by URL right after an earlier designer load (first `04-auto-designer` showed the column). Fixed by `about:blank`, navigate, wait, reload; all shipped screenshots are from fresh loads.
- Portal text wait (`Plan a trip`) never matched, card text is not in the top frame; used fixed waits.
- First navigation hit `/auth/login/` once, a retry loaded the portal without a sign-in.
