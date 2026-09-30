<!-- Do not edit or remove this section -->
<!-- This document exists for non-obvious, error-prone shortcomings in the codebase, the model, or the tooling that an agent cannot figure out by reading the code alone. No architecture overviews, file trees, build commands, or standard behavior. When you encounter something that belongs here, first consider whether a code change could eliminate it and suggest that to the user. Only document it here if it can't be reasonably fixed. -->

---

## Non-obvious constraints

- **Platform mismatch**: Always build with `--platform linux/amd64` for Runpod deployment. Omitting this on ARM hosts (Apple Silicon) produces images that silently fail on Runpod.
