# Aliste Smart Home — Protocol & API Reference

> Reverse-engineered from the official Android app (v52.7, React Native / Hermes
> bytecode) by decompiling `index.android.bundle`. This documents every
> mechanism the app uses to authenticate, enumerate, control, and read the state
> of devices (lights, fans, curtains, locks, IR/Nova, etc.).
>
> Status: **working draft** — sections marked _(verified)_ are confirmed from the
> decompiled source; _(needs confirmation)_ items still need a live capture.

---

## 1. Architecture overview

The app talks to Aliste's cloud over **three** channels, and control commands are
routed to whichever is available, in priority order:

1. **Socket.io** (real-time, authenticated) — preferred when the device is
   reachable (`socketConnected`).
2. **AWS IoT MQTT over WebSocket** (real-time) — used when the device is in the
   MQTT-connected set (`mqttDevices`). Primarily carries **status**; whether raw
   MQTT publish is *authorized to control* depends on the AWS IoT policy.
3. **HTTPS REST** — the cloud fallback / control endpoints.

State/status flows back primarily over **AWS IoT MQTT** (and socket.io).

```
             ┌──────────── HTTPS (login, house fetch, REST control) ───────────┐
 App/SDK ────┼──────────── socket.io  (control emit 'message', status) ────────┼──► Aliste cloud ──► Device
             └──────────── AWS IoT MQTT/WSS (status subscribe, control pub?) ───┘
```

---

## 2. Constants / hosts

| Name | Value |
|------|-------|
| `baseUrl` | `https://web.alistetechnologies.com` |
| `baseUrl2` | `https://a3.alistetechnologies.com` |
| `wssUrl` | `https://a2.alistetechnologies.com/?` |
| `loginUrl` | `https://web.alistetechnologies.com/v2/auth/login` |
| `homeDetailsUrl` | `https://a3.alistetechnologies.com/api/fetch/house2` |
| `commandUrl` (REST control) | `https://a3.alistetechnologies.com/v3/device/control` |
| `subscription/getInfo` | `https://subscriptioncloud.alistetechnologies.com/api/fetch/user/{mobile}?user=` |
| AWS region | `ap-south-1` |
| AWS IoT endpoint | `a1pv71f2h6guqz-ats.iot.ap-south-1.amazonaws.com` (`wss://…`, port 443) |
| Cognito identity pool region | `ap-south-1` |
| Cognito IdentityId | `ap-south-1:4b454fb8-df98-4b94-a5ba-937169c43ad0` |

---

## 3. Authentication _(verified)_

### 3.1 AWS Cognito temp credentials
`POST https://cognito-identity.ap-south-1.amazonaws.com/`
Headers: `x-amz-target: AWSCognitoIdentityService.GetCredentialsForIdentity`,
`content-type: application/x-amz-json-1.1`
Body: `{"IdentityId": "ap-south-1:4b454fb8-df98-4b94-a5ba-937169c43ad0"}`
→ returns `Credentials` = `{AccessKeyId, SecretKey, SessionToken, Expiration}`
(temporary AWS creds used to sign the AWS IoT WebSocket connection). This is an
**unauthenticated** identity pool (fixed IdentityId) — important: the IoT policy
attached to it likely permits *subscribing to status* but may **not** permit
*publishing control commands*.

### 3.2 App login — there are TWO paths

**(a) OTP login (what the phone app uses):**
1. Enter mobile → `POST /v2/auth/initiate` `{mobile, password?, first_name?, last_name?, appHash}`
   (`appHash` = Android SMS-Retriever hash → the server sends an **OTP by SMS**,
   auto-filled by the app; `autoSelectOtp` handles this).
2. Enter OTP → verified (OTP screen posts `{mobile, otp}`; the token exchange goes
   through `/users/v1/otp` / `/v2/auth/login`) → returns `accesstoken` (+ refresh).

**(b) Password login (what the SDK/HA integration uses):**
`POST https://web.alistetechnologies.com/v2/auth/login`
Body: `{"mobile", "password", "deviceToken"}`
→ `{"success": true, "data": {"accesstoken": "<JWT>", "profile": { … }}}`
This works **only if the account has a static password set**. If the account is
OTP-only, or the password was changed, this returns `{"success":false,
"message":"Invalid credentials"}` (HTTP 404). `/v2/auth/{forgotpassword,resetpassword}`
exist to set/reset the password.

`profile` fields: `_id` (the user id used as `?user=` in control URLs), `mobile`,
`email`, `name`/`first_name`/`last_name`, `selectedHouse` (houseId), + prefs.

### 3.3 Token model & refresh (avoids re-OTP)
- `accesstoken` is a **JWT with an `exp` claim**; the app decodes it (`decodeJWT`,
  `isTokenExpired`) and refreshes before expiry.
- Refresh: `GET {oauth}/token/generateAccessToken` (base
  `https://api.oauth.alistetechnologies.com`) with headers
  `{ refreshToken: <token>, client: 'automation', tokenType }` → a fresh access
  token. Also `/token/generateAuthTokenBypassed`, `/token/generateAuthorizeToken`.
- **Implication for a headless SDK:** OTP cannot be automated. Authenticate once
  (password *or* a one-time OTP the user supplies), then **persist the
  refreshToken and refresh via `/token/generateAccessToken`** instead of
  re-logging-in. Relying on `/v2/auth/login` {mobile,password} only works while a
  static password exists and is current.

### 3.4 Auth headers
- Device-control REST calls send an **`accesstoken: <token>`** header (raw token).
- Billing/metering + oauth endpoints use **`Authorization: Bearer <token>`**.

---

## 4. Home / device enumeration _(verified)_

`GET https://a3.alistetechnologies.com/api/fetch/house2/{houseId}/{mobile}`
→ `{ "_id": houseId, "houseName", "rooms": [ { "roomName", "devices": [ … ] } ] }`

### Device object (per physical controller)
Observed fields (example device `S030806`, an MQTT wall device):

| field | meaning |
|-------|---------|
| `deviceId` | device id, e.g. `S030806` — **used in all MQTT topics and control bodies** |
| `_id` | Mongo id |
| `mac` | device MAC (may be empty for MQTT devices) |
| `isMQTTDevice` | `true` → device speaks MQTT (AWS IoT) |
| `ns` | number of switches |
| `css` / `ess` | current / expected switch-state string (one char per switch) |
| `ws`/`cws`/`sent_ws`, `wp`/`sent_wp` | Wi-Fi SSID / password the device is joined to |
| `speed` | fan speed (for fan devices) |
| `firmware_version`, `ota_version`, `strength` (RSSI), `disconnectedAt` | telemetry |
| `switches` | array of switch objects (below) |

### Switch object
`{ switchId, switchName, deviceType, switchState, dimmable, wattage, controllerType?, controllerId? }`
- `switchId` — integer index within the device.
- `switchState` — level on a **0–100** scale (normalise to 0.0–1.0 for on/off).
- `deviceType` — one of the catalog values in §7.
- `dimmable` — whether the switch supports levels.

---

## 5. AWS IoT MQTT connection _(verified)_

- Endpoint: `a1pv71f2h6guqz-ats.iot.ap-south-1.amazonaws.com`, `wss://`, port **443**,
  transport **websockets**.
- The WebSocket URL is **SigV4-signed** using the Cognito temp credentials
  (AccessKeyId / SecretKey / SessionToken). The signed `Host` header must equal
  the websocket netloc.
- Implemented in the SDK via `aws_signv4_mqtt.generate_signv4_mqtt(...)` + `aiomqtt`.

---

## 6. Control & status — see agent sections below

The exact HTTP endpoints (§8), MQTT topics (§9), and socket.io events (§10) are
compiled from the exhaustive extraction and inserted below. Key facts established
so far:

### 6.1 Control-path decision _(verified)_ — function `send_message(deviceId, switchId, command)`
```
if socketConnected.includes(deviceId):        → socket.emit('message', { … })      # preferred
elif mqttDevices.includes(deviceId):          → mqttControl(...)                    # AWS IoT MQTT
else:                                          → toast "Not connected locally/online"
```
- `mqttControl` publishes to **`control/{deviceId}`** (string `"{switchId},{level},{id}"`)
  **and** **`command/{deviceId}`** (JSON, see §9), then optimistically updates local state.
- REST `callUpdate` → `POST /v3/device/control` is a separate path (see §8).
- **Command level scale: 0–100** (100 = on, 0 = off); dim = level. Internally the
  app normalises reported states >1 by dividing by 100.

### 6.2 Status decision _(verified)_
- On (re)connect the app subscribes per-device to status topics and **publishes an
  empty message to `status/{deviceId}`** to pull current state (`syncStatesOfRoom`).
- Realtime switch changes arrive on **`message/{deviceId}`** = `{sid, s}`.
- Bulk/初始 sync arrives on **`e/sync/{deviceId}`** / **`e/conn/{deviceId}`** = `{ls:[…]}`.
- Device online/offline via AWS presence topics
  `$aws/events/presence/connected|disconnected/{deviceId}`.
- House-wide connected set via **`housedevices/{houseId}`**; the app also publishes
  **`app/gethousedevices/{houseId}`** to request it.

---

## 7. Device type catalog _(verified)_

`ControlTypeMap = { Relay: 0, Wattage: 1, Current: 2 }` (per-switch control type;
Relay = on/off switch, Wattage/Current = energy-metering sync devices).

Device categories (`DeviceTypes`): `SYNC`, `CURTAIN`, `IRBLASTER`, `LOCK`, `NVR`,
`IPCAMERA`, `NOVA` (IR blaster), `MOTION_SENSOR`, `RGB`, `LOCK_HUB`, `WAVE_SENSOR`,
`TTLOCK`, `TTGATEWAY`, `WIZ`, `STACKS`, `RGBWWW`, `TUYA_CAMERA`, `NOVA_SYNC`,
`HONEYWELL_THERMOSTAT`, plus energy category `EnergISync`.

Switch/appliance types (`deviceType` on a switch): `FAN`, `AC`, `BULB`, `CFL`,
`SOCKET`, `GEYSER`, `TUBELIGHT`, `TWO_WAY`, `SCENE`, `TV`, `SPEAKER`, `FOUNTAIN`,
`BEDSIDE_LAMP`, `CHANDELIER`, `STRIP_LIGHT`, `STUDY_LAMP`, `LAMPS`,
`FAN_REGULATOR`, `LIGHT`, … Each has flags `{dimmable, isFanRegulator}`.

- **Lights** (BULB/CFL/TUBELIGHT/CHANDELIER/…): on/off; some `dimmable` (0–100 level).
- **Fan / FAN_REGULATOR**: speed as a 0–100 level.
- **Curtain**: open/close/position (see §8/§9).
- **Lock / TTLOCK**: lock/unlock.
- **Nova / IRBLASTER**: IR codes via socket `nova_control` / `generateNovaCommand`.
- **RGB / RGBWWW**: color/brightness.

---

## 8. HTTP/REST endpoint reference _(verified)_

**Hosts:** `baseurl` (main API, `web`/`a3`), `socketurl` = `https://a3.alistetechnologies.com:443`,
`wssurl` = `https://a2.alistetechnologies.com:443`, `subscriptionCloud` =
`https://subscriptioncloud.alistetechnologies.com`, plus microservices on
`*.aliste.io` and per-sensor `*.execute-api.ap-south-1.amazonaws.com` lambdas.

**Auth:** most calls send header **`accesstoken: <token>`** (from `/v2/auth/login`).
Billing/metering (`smartmeter.aliste.io`) uses `Authorization: Bearer <token>` from
`/token/generateAccessToken`. Control calls also carry `?user=<email>&invoker=app&time=<epochMs>`.

### Core endpoints
| Method | URL | Body / notes |
|--------|-----|--------------|
| POST | `/v2/auth/login` | `{mobile, password, deviceToken}` → `{data:{accesstoken, profile:{_id, email, selectedHouse, …}}}` |
| GET | `/api/fetch/house2/{houseId}/{mobile}` | full house/rooms/devices/switches tree |
| POST | `https://a3.alistetechnologies.com/v3/device/control` | **REST control** — `{deviceId, switchId, command, controllerType, controllerId, controllerDetails:{}}` |
| POST | `{socketurl}/api/sendCommand?user=<email>&invoker=app&time=<ms>` | legacy control — `{deviceId, switchId, command}` |
| POST | `{socketurl}/v2/house/syncStates` | `{deviceIds}` — pull current device states |
| GET | `/v2/scenes/activate/…?user=` / `/v2/scenes/deactivate/…` | run scene |
| GET | `/v2/schedule/toggle/…`, `/v2/geoscenes/toggle/…` | enable/disable |

### Endpoint families (CRUD; base `baseurl` unless noted)
- **Auth/User:** `/v2/auth/{login,signup,initiate,forgotpassword,resetpassword,third_party}`, `/v2/user/{logout,profile,delete,feedback,update_device_token,set_*_notification,set_haptic_feedback}`, token svc `/token/*` (`api.oauth.aliste…`), `/users/v1/*` (`services.user.aliste.io`).
- **House/Devices:** `/api/fetch/{house2,user,keys}`, `/api/update/{selectedHouse,roomLayout}`, `/api/deleteDevice`, `/v2/house/{room,rename,delete,address,add/userWithAccess,change_owner,updateTuyaDetails,wifi}`.
- **Control/State:** `/v3/device/control`, `/api/sendCommand`, `/v2/house/syncStates`, `/v2/analytics/get_logs`.
- **Scenes/GeoScenes:** `/v2/scenes/{activate,deactivate,house,CRUD}`, `/v2/geoscenes{,/toggle}`.
- **Schedules/Timers:** `/v2/schedule/*` & `/v3/centralschedules/*` (subscriptionCloud), `/v2/timer/*`, `/device/{setTimer,enableAutoOff,…}` (device lambda).
- **Device mgmt:** `/v2/device/{verification,setup/switches,change/{wifi,switches,room},update_devices_wattage,update_device_password}`, `/v3/device/updateAppliance`.
- **Curtains:** `/v2/curtain/{verification,rename,delete,change/wifi,change/room,set_password}`.
- **Locks:** `/v2/ttlock/{add,check,delete,rename,generate_otp,remote_unlock,initialize}`, `/v2/ttgateway/*`, `/v2/locks/{add,verify,delete}`, `/v3/lockFingerPrint/*`, `/v3/lockPasscode/*`.
- **Nova/IR:** `/v2/nova/{add,verification,update,delete,add_custom_remote,getRemoteBrands}`, `/v2/irRemote/{addRemote,remotes,companies,copyToNova}`.
- **Sensors:** `/v2/motionsensor/*`, `/v2/wavesensor/*`, `/v3/doorsensor/*`, `/v3/weathersensor/*`, `/v3/thermosmart/*`, `/v3/environsync/*`, `/v3/energiSync/*`.
- **Other integrations:** `/v2/{rgb,rgbwww,wiz/add,homebridge,honeywellThermostat,stacks,nvr,tuyaCamera}`.
- **Billing/metering:** `smartmeter.aliste.io` `/room/*`, `/app/*` (Bearer auth); global meters `/v3/globalMeters/*`.
- **Subscription/referral:** `/v3/referral/*`, `/api/tele/*` (subscriptionCloud/web).

> A full per-endpoint dump (every device-family CRUD verb) is available in the
> extraction notes; the table above covers everything needed to control and read
> devices. Long-tail management endpoints follow `add/verification=POST`,
> `rename/change=PUT`, `delete=POST/DELETE`.

## 9. MQTT topic reference _(verified)_

All MQTT I/O goes through aws-amplify `iotProvider` (`publish({topics:[t], message})`).
MQTT is primarily a **status** channel; control emitted here is not reliably
actuated for unauthenticated identities (see §11).

### PUBLISH (app → broker)
| Topic | Payload | Purpose |
|-------|---------|---------|
| `control/{deviceId}` | string `"{switchId},{command},{id}"` | control (command 0–100; `id` = `String(Date.now()).slice(5,13)`) |
| `command/{deviceId}` | JSON `{deviceId, switchId, command, id, controllerType:"app", controllerId:"name(email)", controllerDetails:"{}"}` | control w/ attribution (published alongside `control/`) |
| `status/{deviceId}` | `{}` | request current state (device replies on `e/sync/`) |
| `app/gethousedevices/{houseId}` | `{}` | request house device list (reply on `housedevices/`) |

### SUBSCRIBE (broker → app)
| Topic | Payload | Effect |
|-------|---------|--------|
| `housedevices/{houseId}` | JSON **array** of `{deviceId,…}` | marks devices MQTT-connected; triggers resync |
| `message/{deviceId}` | `{sid, s}` | single-switch live update → `localStateUpdate(deviceId, sid, s)` |
| `e/sync/{deviceId}` | `{ls}` | full resync → `syncStates` |
| `e/conn/{deviceId}` | `{ls}` | full state on connect → `syncStates` |
| `$aws/events/presence/connected/{deviceId}` | (body ignored) | device online |
| `$aws/events/presence/disconnected/{deviceId}` | (body ignored) | device offline |
| `es/ev/message/{deviceId}`, `es/ev/readings/{deviceId}` | full msg | EnergISync meter devices |

**Payload details (verified against the reducers):**
- `message` `{sid, s}`: `sid` = switchId, `s` = state value (0–100 scale).
- `e/sync` / `e/conn` `{ls}`: **`ls` is a comma-separated STRING of numbers**, e.g.
  `"0,100,0,1"`. The reducer does `ls.split(',').map(Number)` and assigns by
  **positional index = switchId**: `switch.switchState = arr[switch.switchId]`.
  (⚠️ NOT an array of `{sid,s}` objects — the SDK must parse it as a positional string.)
- Levels use the same 0–100 scale as commands; normalise >1 by /100 internally.

Routing: `$aws…` → presence; `es/ev/…` → energiSync; strip leading `e/` then split
`/` → `[type, deviceId]` where type ∈ {`conn`,`sync`,`message`}.

## 10. socket.io reference _(verified — this is the primary control channel)_

**This is how the app actually controls devices.** MQTT (AWS IoT) is mainly a
status channel; realtime control is emitted over socket.io.

### 10.1 Connection
- **URL:** `https://a2.alistetechnologies.com:443` (the config value `wssurl`).
  (Note: `socketurl` = `https://a3.alistetechnologies.com:443` is used for plain
  HTTPS REST only, e.g. `POST /v2/house/syncStates` — do NOT point the socket at a3.)
- socket.io-client, default path **`/socket.io`**, default namespace `/`.
- Options: `{ reconnect: true, secure: true, transports: ['websocket'] }` (websocket only).
- **Auth is via query params only** (no bearer token):
  `{ node_mcu_id: 'auto1001', device_type: 'phone', house_access_code: <house._id>, user: <user.email> }`
- Created in `socketcon(houseAccessCode)`; re-created when the selected house changes.

### 10.2 REQUIRED join step (why control silently fails without it)
On the socket `'connect'` event, the app immediately emits:
```
socket.emit('device_connection_update', { houseAccessCode: <user.selectedHouse> })
```
The server replies on the `'device_connection_update'` **listener** with the
connected-device roster, which populates `socketConnected`. **Until this roster
arrives, `socketConnected` is empty and every control call falls through to
MQTT/"Not connected locally/online".** This is the leading cause of control
failure when talking to the socket directly.

### 10.3 Emitted events
| Event | Payload | Purpose |
|-------|---------|---------|
| `device_connection_update` | `{ houseAccessCode: <selectedHouse> }` | join/subscribe (emit on connect) |
| **`message`** | `{ deviceId, device_type:'phone', command, switchId, user:<email>, username:<name or first+last>, id:<String(Date.now()).slice(5,13)> }` | **switch/fan control** (see command-scale note ⚠️) |
| `nova_control` | `{ deviceId, payload: generateNovaCommand(code) }` | IR/Nova blaster |
| `motion_sensor_enabled` | `{ deviceId, enabled, id }` | toggle motion sensor |
| (generic) `emitEventOnSocket(name, payload)` | — | passthrough for other events |

> ⚠️ **Command scale (socket vs MQTT differ).** `send_message` passes the **raw**
> command to the socket `message` event, but scales it (`if 0<cmd≤1: cmd*=100`)
> before the MQTT/`mqttControl` path. Verified: **OFF = `0`** on both. The ON /
> dim value is computed in the (tangled) switch/dimming handler — socket carries
> the pre-scale value (likely `1` for a plain on/off switch, or the 0–100 level
> for a dimmer), MQTT carries the 0–100 value. **Confirm the exact ON value with a
> live socket capture before relying on it.**

`generateNovaCommand(cmd, repeat)` returns a **comma-joined string**, not an
object: decoded mode → `"{repeat},{decode_type},{value},,{nbits},,{repeat}"`;
raw mode → `"{repeat},{decode_type},…,{rawLength},,{rawData}"`.

### 10.4 Listened events (status/state come back here too)
| Event | Payload → effect |
|-------|------------------|
| `connect` | mark connected; **emit `device_connection_update`** |
| `disconnect` / `connect_error` | mark disconnected; clear connected lists |
| **`message`** | `{ deviceId, switchId, state }` → `localStateUpdate` (state echo; `state` numeric) |
| `conn_update` | string `"{deviceId}-{online|offline}"` → per-device connection status |
| `resync_states` | → `updateStates(payload)` |
| `device_connection_update` | JSON: `{ devices:[…], motionsensors, novas, rgbs, lockhubs, rgbwwws }` → populates `socketConnected = devices` |
| `curtain_sync_states` / `curtain_state_update` | curtain state |
| `nova_conn_update` / `nova_power_update` | `"id-online"` / `{deviceId, remote_id, power_state}` |
| `motion_sensor_conn_update` / `motion_sensor_toggled` | motion sensor |
| `rgb_update` / `rgb_www_update` / `rgb_conn_update` | RGB devices |
| `lock_hub_update` / `lock_hub_conn_update` | `{deviceId, mounting, lock, door, battery, lock_connected}` |
| `honeywell_conn_update` / `honeywell_thermostat_sync` | thermostat |

### 10.5 Reachability lists
- `socketConnected` (checked by `send_message`) = `payload.devices` from the
  `device_connection_update` reply; incrementally updated by `conn_update`
  (`"deviceId-online/offline"`).
- `mqttDevices` / `mqttConnected` is a **separate** list fed by the MQTT client
  (`housedevices/{houseId}` + presence), not the socket.

### 10.6 Implication for the SDK
The SDK currently controls over MQTT/REST only. To match the app it must:
1. Open a socket.io connection to `wss://a2.alistetechnologies.com:443` with the
   query params above.
2. Emit `device_connection_update{houseAccessCode}` on connect.
3. Emit `message{deviceId, device_type:'phone', command(0–100), switchId, user, username, id}` to control.
4. Listen to `message` / `conn_update` / `resync_states` (+ curtain/lock/etc.) for state.

---

## 11. Root cause of control failure (resolved) + fix plan

**Root cause:** the SDK was controlling over the wrong channel. The app's primary
control channel is **socket.io to `wss://a2.alistetechnologies.com:443`** (§10),
emitting `message`. The SDK only tried AWS IoT MQTT (`control/`+`command/`) and
REST — MQTT (unauthenticated Cognito identity) is a **status** channel and does
not actuate the device, and the REST endpoint returns `{"success":true}` without
reliably routing to the device. That is why every control attempt failed while
status/connection at least partially worked.

**Fix plan (for the SDK rewrite, after this doc is reviewed):**
1. Add a **socket.io client** → `wss://a2.alistetechnologies.com:443`, websocket
   transport, query `{node_mcu_id:'auto1001', device_type:'phone', house_access_code:<houseId>, user:<email>}`.
2. On connect, emit `device_connection_update{houseAccessCode:<houseId>}` and wait
   for the roster (`socketConnected`).
3. Control by emitting `message{deviceId, device_type:'phone', command:0–100, switchId, user:<email>, username:<name>, id}`.
4. Consume `message` / `conn_update` / `resync_states` for live state; keep AWS
   IoT MQTT as a secondary status source and REST as an offline fallback.
5. Device-type specifics: curtains via `curtain_*` events, Nova/IR via
   `nova_control`, locks/RGB/thermostat via their respective events (§10.4).

### ⚠️ Important reframe: the device may simply be OFFLINE
Every control attempt (REST returned `{"success":true}`, MQTT publish accepted,
socket never had the device in `socketConnected`) is **also fully explained by the
target device being offline** — its cloud record was last updated 2025-12-02
(~7 months stale). If the device is offline, *no* channel works, and we cannot yet
prove that socket.io is strictly required rather than just the app's preference.
**Do not assume the channel was the bug until the device is confirmed online.**

### Pre-implementation checklist (do these in order)
1. **Confirm the device is online** — does the official Aliste app control it right
   now? If not → it's a Wi-Fi/power issue; stop, no code will help.
2. **Get working credentials** — login currently returns `Invalid credentials`
   (password may have been rotated). The HA integration will also need the updated
   password.
3. **Once online, test cheapest-first:** REST `POST /v3/device/control` with
   `command:100` + full body (`controllerType`,`controllerId`,`controllerDetails`)
   and the `accesstoken` header — this may already work. Then MQTT. Only build the
   socket.io client if those don't actuate.
4. **If socket.io is needed:** use `python-socketio` (Engine.IO v4 — verified),
   connect to `wss://a2.alistetechnologies.com:443` with the query params (§10.1),
   emit `device_connection_update{houseAccessCode}` on connect, then confirm the
   device appears in the `device_connection_update` roster before emitting
   `message`. Capture one real app command to lock the exact `command` value.

### Still to confirm with a live capture (not blocking the doc)
- Exact socket `message` `command` value for ON / dim (§10.3).
- `resync_states` payload shape.
- Whether the socket needs any auth beyond the `user`/`house_access_code` query
  params (the decompile shows none, which is worth double-checking on the wire).
