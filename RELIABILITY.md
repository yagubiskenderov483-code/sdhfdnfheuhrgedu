# Tracker reliability patch

Application changes are committed as source files. No workflow or startup patch script rewrites the application. Core modules retain the previous transport, scheduling, database, startup/shutdown and captcha implementations; the public modules apply the stricter policy.

## Behavior

- Anonymous/unverified owners are rejected. A name match or a remembered name-to-user record is not proof of NFT ownership. The lookup path checks the candidate's current profile, username and the exact gift in visible inventory. Unreadable/private inventories are skipped, not guessed.
- Original detection age survives lookup, retry, queue and delivery waits. Strict mode caps detection-to-delivery age at 180 seconds. This is not the true listing age: the source response does not supply a listing timestamp.
- Immediately before sending, validate active sale, gift identifier, price in the primary currency, and seller identity. Validation failure or uncertainty rejects the offer. A delivery timeout is not blindly retried because Telegram may already have accepted it.
- Strict synchronization reads a baseline and does not promote unknown rows below anchors, weak-owner reprices or count-only guesses. An incomplete baseline is kept quiet. This favors accuracy over volume and can skip real listings; startup synchronization can take longer.
- Lookup failures produce rate-limited warnings, without changing the configured primary or marking every account unavailable. Explicitly configured fallback bots may still serve individual requests.
- At markup >=333%, require a rare model/backdrop or Monochrome. Numeric rarity defaults to <=1.5%; Rare/Epic/Legendary labels also qualify. A rare pattern alone is not an exception. Existing configured profile-language rules are no longer bypassed after lookup success; no ethnicity inference is added.
- Keep a canonical NFT link in the message and Lot button independently of Telegram previews. Do not present the trade bot as the seller in a fallback keyboard.
- Acknowledge slow control requests, suppress identical in-flight controls, and surface handler errors without automatically replaying state-changing commands.

## Validation and limits

`python tests/test_reliability.py -v` checks URL/username validation, age accounting, markup boundaries, rarity exceptions and source syntax. Eight helper tests passed during preparation. The final syntax/retained-core check is also included for CI. Full dependency-backed regression tests and live Telegram behavior have not been verified in this editing environment; GitHub Actions previously failed before running steps. Do not treat a commit or a passing helper test as deployment verification.

Existing tests that expect name-only ownership, global dead marks or permissive old-listing recovery describe the former behavior. They remain in the repository rather than hiding failures. Deployment and broader integration review are separate steps.
