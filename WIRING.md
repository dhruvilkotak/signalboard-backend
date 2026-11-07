# B0 / F0 — feature flags: where each file goes and what to wire

## Backend (`signalboard-backend`, issue #47)

New files, copy as-is:

| File | Purpose |
|---|---|
| `services/feature_flags.py` | `FeatureFlagService`: Firestore read, 60 s cache, `is_enabled(name, uid)` |
| `routers/config.py` | `/api/config`, `/api/config/public`, admin GET/PUT |
| `scripts/seed_feature_flags.py` | Creates `config/feature_flags` with every flag `off` |
| `tests/test_feature_flags.py` | Unit tests with a fake Firestore |

### 1. `routers/config.py`: auth import

Replace this line with your real auth dependencies:

```python
from utils.auth import get_current_user, require_admin
```

`get_current_user` must return a dict with `"uid"`; `require_admin` must 403 non-admins.
Until B1 adds roles, `require_admin` is your existing admin-UID check.

### 2. `main.py`: inject the service (no `from main import` in routers)

```python
from services.feature_flags import FeatureFlagService
from routers import config as config_router

flag_svc = FeatureFlagService()          # after firebase_admin.initialize_app(...)
config_router.flag_svc = flag_svc
app.include_router(config_router.router)
```

Later routers that need flags get the same instance:
`some_router.flag_svc = flag_svc`, then `flag_svc.is_enabled("paywall", uid)`.

### 3. Firestore rules

The backend uses the Admin SDK, which ignores rules. Lock out clients:

```
match /config/feature_flags {
  allow read, write: if false;
}
```

### 4. Seed and test

```bash
python scripts/seed_feature_flags.py   # on the VM, venv active
pytest tests/test_feature_flags.py     # locally
```

### Note

The design doc lists `PUT /api/admin/feature-flags`. This code uses
`PUT /api/admin/feature-flags/{name}` (one flag per call), which is simpler for the admin UI.

## Frontend (`signalboard-frontend`, part of issue #32)

| File | Purpose |
|---|---|
| `src/hooks/useFeatureFlags.js` | Zustand store + `useFlag(name)` + `<Flag name>` |
| `src/components/admin/FeatureFlagsPanel.jsx` | Admin tab to change modes and allowlists |

### 1. `src/lib/api.js`: add three functions

Use the same request helper that `getMarketStatus` uses (it attaches the Firebase token):

```js
export const getConfig = () => request('GET', '/api/config');
export const getAdminFeatureFlags = () => request('GET', '/api/admin/feature-flags');
export const updateFeatureFlag = (name, body) =>
  request('PUT', `/api/admin/feature-flags/${name}`, body);
```

(`request` = whatever your helper is called.)

### 2. Load flags once in the signed-in tree

```jsx
import { useFeatureFlagsLoader, useFeatureFlagStore } from './hooks/useFeatureFlags';

function SignedInApp() {
  useFeatureFlagsLoader();
  ...
}
```

On sign-out, call `useFeatureFlagStore.getState().reset()`.

### 3. Admin portal

Add a **Feature flags** tab to the Admin page that renders `<FeatureFlagsPanel />`.

### 4. Using a flag

```jsx
const billingOn = useFlag('billing');
<Flag name="billing"><Link to="/pricing">Upgrade</Link></Flag>
```

## Done when

- [ ] Seed script run on prod; all 7 flags show `off` in the admin tab
- [ ] Your UID in `billing` allowlist → `GET /api/config` shows `billing: true` for you only
- [ ] Non-admin gets 403 on `/api/admin/feature-flags`
- [ ] `pytest` passes
