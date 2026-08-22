# Security policy

This repository is public and must remain strategy-neutral.

Never commit credentials, account identifiers, private strategy modules,
research datasets, model parameters, strategy configuration, derived results,
or production databases. The ignore rules are defense in depth, not permission
to stage files blindly.

Before publishing, scan the complete Git history as well as the current tree.
If sensitive material ever enters a public commit, remove the reachable history
and rotate any affected credential; deleting it later is not enough.

