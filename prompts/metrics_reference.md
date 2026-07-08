# Garmin Metrics Reference — Garmin Watch → AgBot

What the watch actually measures/computes, what each number means, how to read it, the
`garminconnect` method that exposes it, and whether **AgBot** now uses it. Written as
generic agent knowledge; example values are illustrative, not personal data. Most advanced metrics are **Firstbeat
Analytics** models (Garmin owns Firstbeat). This doubles as agent knowledge — read it
before changing the data layer or prompts.

Legend: ✅ used by the bot · ➕ added this round · ⚪ available, not used (low value for
the user's goals) · ❌ empty/not available on the user's account.

---

## 1. Daily recovery & readiness

| Metric | Means | How to read | Source | Bot |
|---|---|---|---|---|
| **Training Readiness** | 0–100 composite of sleep, recovery time, HRV, acute load, stress & sleep history | HIGH = train hard; LOW/VERY_LOW = go easy | `get_training_readiness` | ✅ |
| ↳ sub-factors | Each input's % contribution + feedback (sleepScore, recoveryTime, hrv, acwr, stressHistory, sleepHistory) | Tells you *why* readiness is low | same | ➕ (now surfaced) |
| **Recovery Time** | Hours until fully recovered for a hard session | Train easy while it's high; e.g. ~24-30 h after a hard day | `get_training_readiness.recoveryTime` (minutes) | ➕ |
| **Body Battery** | 0–100 energy reserve (Firstbeat), charges with rest/sleep, drains with stress/activity | Use the **current** value, not the day's low. Charged >75 good; <25 depleted | `get_stats.bodyBatteryMostRecentValue` (canonical = Connect app/web); snapshot stamps `body_battery_current_as_of` + `_age_min` | ✅ (canonical + freshness) |
| **Stress** | 0–100 from HRV (low HRV = high stress). Rest/low/med/high | Sustained >50 = strained; want long low-stress/rest periods | `get_stats.averageStressLevel`, `get_stress_data` | ✅ (avg/max) |

## 2. Sleep

| Metric | Means | How to read | Source | Bot |
|---|---|---|---|---|
| **Sleep Score** | 0–100 quality (duration, stages, restfulness, timing, HRV) | ≥80 good, <60 poor | `get_sleep_data.sleepScores.overall` | ✅ |
| **Stages** | Deep / Light / REM / Awake seconds | Deep = physical recovery, REM = mental; want ~1.5–2 h deep+REM | `get_sleep_data` DTO | ✅ |
| **Overnight HRV / RHR** | Avg HRV & resting HR during sleep | Elevated RHR or low HRV = under-recovered / incoming illness | `get_sleep_data` | ✅ |
| **Sleep SpO2 / Respiration** | Overnight blood-oxygen & breaths/min | SpO2 dips or high respiration = poor sleep/altitude/illness | sleep DTO | ✅ (may be null if pulse-ox is off) |
| **Skin Temperature** | Overnight skin-temp **deviation** from a rolling ~19-night baseline (°C/°F) | Not absolute — a *change*. Sustained **+0.5°C or more**, esp. with raised RHR / low HRV / high respiration = illness, alcohol, heat or under-recovery. e.g. -0.2°C (normal) | `get_sleep_data.avgSkinTempDeviationC/F` + `skinTempCalibrationDays` | ➕ (illness/strain flag) |
| **Restless Moments** | Count of movement events during sleep | More = more fragmented sleep; e.g. 49 | `get_sleep_data.restlessMomentsCount` | ➕ |
| **Breathing Disruptions** | Count of breathing-variation events overnight | Elevated = possible sleep-disordered breathing. `255` is a "no reading" sentinel (Connect shows `--`) → ignored | `get_sleep_data.breathingDisruptionData` | ➕ |

## 3. HRV status

| Metric | Means | How to read | Source | Bot |
|---|---|---|---|---|
| **HRV Status** | 7-day overnight HRV vs personal baseline: BALANCED / UNBALANCED / LOW / POOR | BALANCED = recovered. Below baseline = fatigue, stress, illness, alcohol | `get_hrv_data.hrvSummary` | ✅ |
| **Baseline band** | Your normal low/balanced range (ms) | Compare last-night avg to `balancedLow`; below = red flag | same | ✅ (used by red-flag alert) |

## 4. Training load & status (Firstbeat)

| Metric | Means | How to read | Source | Bot |
|---|---|---|---|---|
| **Training Status** | PRODUCTIVE / MAINTAINING / RECOVERY / UNPRODUCTIVE / OVERREACHING / DETRAINING / PEAKING | The headline verdict on whether training is paying off | `get_training_status` | ✅ |
| **Acute load** | ~7-day rolling training load (EPOC-based) | Short-term fatigue | `...acuteTrainingLoadDTO.dailyTrainingLoadAcute` | ➕ |
| **Chronic load** | ~28-day load = fitness base | The bigger, the more you can absorb | `...dailyTrainingLoadChronic` | ➕ |
| **ACWR** (acute:chronic) | Ratio of the two | **0.8–1.3 optimal**, >1.5 spike (injury risk), <0.8 detraining. e.g. ~1.4 | `...dailyAcuteChronicWorkloadRatio` | ➕ |
| **Load Focus** | 4-week load split into anaerobic / high-aerobic / low-aerobic vs Garmin target ranges, + balance verdict | Shows imbalance & what to add; e.g. aerobic-low over target with comparatively lower high-aerobic/anaerobic load → example focus verdict | `training_status.mostRecentTrainingLoadBalance` | ➕ (steers session type) |
| **Training Effect** | Per-activity **Aerobic** & **Anaerobic** 0.0–5.0 | 1 minor · 2–3 maintaining/improving · 4 highly improving · 5 overreaching | activity `aerobic/anaerobicTrainingEffect` | ✅ (in activities) |

## 5. Performance suite — the big gap we just closed (slow-changing) ➕

| Metric | Example | Means / how to read | Source | Bot |
|---|---|---|---|---|
| **VO2max** | e.g. ~40 | mL/kg/min — aerobic ceiling. Drives fitness age + race predictions. Higher = fitter | `get_training_status.mostRecentVO2Max` | ✅ |
| **Fitness Age** | e.g. below/above chronological age, with achievable target | Fitness expressed as an age; below chronological = good; "achievable" = target | `get_fitnessage_data` | ➕ |
| **Race Predictions** | e.g. predicted 5K / 10K / half / marathon times | Modeled times from VO2max + training. **Assume race-day form** — optimistic if untrained for the distance | `get_race_predictions` (seconds) | ➕ |
| **Endurance Score** | e.g. score + band | Ability to sustain effort. Bands: <5100 Novice · 5100 Intermediate · 5800 Trained · 6600 Well-Trained · 7300 Expert · 8100 Superior · 8800 Elite | `get_endurance_score` | ➕ |
| **Hill Score** | e.g. 20 | 0–100 climbing strength (strength + endurance blend). Teens/low-20s = entry band | `get_hill_score` | ➕ |
| **Lactate Threshold HR** | e.g. Garmin-estimated bpm | HR sustainable ~1 h; anchor tempo/threshold runs near it | `get_lactate_threshold` | ➕ (HR only; pace unit unreliable) |
| **Cycling FTP** | e.g. FTP watts, possibly stale | ~1 h sustainable power; sets bike zones. If stale, suggest a retest | `get_cycling_ftp` | ➕ |
| **Weekly Intensity Minutes** | e.g. total / 150 goal | WHO activity target; vigorous counts **double**. Compare total to the goal | `get_intensity_minutes_data` | ➕ |

## 6. Activity granularity

| Metric | Means | How to read | Source | Bot |
|---|---|---|---|---|
| **Exercise Sets** (strength) | Per-set exercise, reps, weight the watch auto-detects | Real gym log — e.g. exercise, sets, reps and detected weight. Drives progression coaching + muscle-group avoidance | `get_activity_exercise_sets` | ✅ (strength debriefs + last 3 strength sessions in daily snapshot) |
| **Splits / HR zones / power zones** | Per-lap + time-in-zone | For deep run/ride analysis | `get_activity_splits`, `..._hr_in_timezones` | ➕ (time-in-zone in every workout debrief) |
| **Activity Weather** | Temp / feels-like / humidity / wind / conditions at activity time | Heat & humidity context for pace/HR; outdoor only (indoor → null) | `get_activity_weather` | ➕ (outdoor debriefs) |
| **Personal Records** | Fastest 1K/1mi/5K/10K, longest run/ride, most steps… | Motivation / goal-setting; "close to a PR" nudges | `get_personal_record` | ➕ (best-effort typeId label map) |

## 7. Whole-day wellness — the `get_user_summary` mega-pull ➕

`get_user_summary(date)` is Garmin's **richest single call** and now feeds a `wellness_today`
block in every snapshot:
- **Movement budget:** sedentary / active / highly-active / sleeping **hours** (e.g. unusually high
  sedentary time can be a strain signal on rest days).
- **Stress duration split:** rest / low / medium / high / activity-stress **minutes** +
  `stressQualifier` (BALANCED) — richer than the single avg-stress number.
- **RHR trend:** `restingHeartRate` today vs `lastSevenDaysAvgRestingHeartRate` —
  a multi-day rise is an early fatigue/illness flag.
- **Body-battery accounting:** charged / drained / during-sleep / at-wake.
- **Respiration** (`get_respiration_data`): avg sleep / waking / high / low.
- **Sweat loss** (`get_hydration_data.sweatLossInML`) drives a post-sweaty-session
  rehydration nudge even with zero logged intake.
- **Global freshness:** `lastSyncTimestampGMT` → snapshot `last_sync_gmt` / `last_sync_age_min`.
- **Morning readiness** (`get_morning_training_readiness`): the wake-time recovery verdict
  (score / label / feedback), preferred for the 09:30 brief since current-moment
  readiness decays through the day.

Base `get_stats` still supplies: steps + goal ✅ · floors ✅ · distance ✅ · calories ✅ · RHR ✅.

## 8. Empty / off on the user's account (don't bother)
`get_max_metrics` ❌ (VO2max comes via training_status) · `get_running_tolerance` ❌ ·
SpO2 / pulse-ox ❌ (feature disabled on the watch) · menstrual, pregnancy, blood-pressure,
golf, gear, badges — ⚪ not relevant. **Hydration is now used** (sweat loss). **Skin temp is
NOT missing** — it lives in the sleep DTO (see §2), an earlier note wrongly said the watch
doesn't expose it.

## 9. Coverage vs the official Garmin Connect Developer Program
The official program (developer.garmin.com) splits into **Health API** (HR, sleep, steps,
stress, respiration, body composition, pulse-ox, HRV, **skin temp**, hydration), **Activity
API** (full workouts + raw **.FIT/GPX/TCX** files), and **Women's Health API** (menstrual /
pregnancy). It's a partner/OAuth push program — the bot can't call it — but every data *domain*
it exposes is already reached by `python-garminconnect` (same Connect cloud). The one genuinely
deeper layer is **raw FIT per-second streams** (intra-workout HR/pace/power/cadence/GPS), which
`garminconnect` *can* download (`download_activity`) — a future option for HR-drift / pacing-fade
analysis, not needed for daily coaching.

---

## What changed this round
- **Added** a cached `fitness_profile()` (fitness age, race predictions, endurance/hill
  score, LT-HR, FTP, weekly intensity) injected into every answer + a `performance`
  command.
- **Added** `exercise_sets()` → strength debriefs cite real logged sets/reps/weight, and
  the last ≤3 strength sessions carry `logged_sets` in the daily snapshot (cached
  immutably by activity-id) so workout recs avoid re-hitting just-trained muscles.
- **Added** a `COACHING DIRECTIVE` injected into every generation path (summary, Q&A,
  image, weekly, nutrition, performance, debrief, red-flags) that forces the model to
  weigh ALL signals together (recovery + load + fitness + recent sessions/sets + body +
  notes), cross-check them, then report tightly naming the 2–3 drivers.
- **Enriched** readiness (recovery-time hours + all sub-factors) and training-status
  (ACWR ratio, acute/chronic load, load trend) in the daily snapshot.
- **Fixed** body-battery *current*: now reads Garmin's canonical `bodyBatteryMostRecentValue`
  (identical to the Connect app/web) instead of scraping the last point of the sparse
  `get_body_battery` series, and the snapshot now carries `body_battery_current_as_of` /
  `_age_min` so the bot reports it **honestly with its freshness** — flagging the "as of" time
  only when the reading is stale (>~15 min). Root cause of prior mismatches was watch→cloud
  sync lag (API is as fresh as Connect web, both trail the wrist until a sync), not a bug.
- **Fixed** latest weigh-in: `get_body_composition` returns `dateWeightList` **newest-first**,
  so taking `dwl[-1]` reported the *oldest* entry in the window (e.g. "early June"). Now picks
  the most-recent by date (`max` on `calendarDate`), and the weekly `weight_series` is sorted
  oldest→newest for a natural trend read.

## What changed — data-utilization pass (all-endpoints audit)
Cross-referenced all 94 `garminconnect` methods + the official Garmin Health/Activity API
catalog against Whoop/Fitbit-Air AI features, then wired the untapped data in:
- **Tier 1 (daily snapshot):** new `wellness_today` block from `get_user_summary` (sedentary/
  active hours, stress-duration split, RHR trend, BB accounting), `get_respiration_data`,
  `get_hydration_data` (sweat loss); **skin-temp deviation + restless moments + breathing
  disruptions** in the sleep block; **Load Focus** split in training-status; **morning
  readiness** (wake-time) for the 09:30 brief; a global `last_sync_gmt` / `_age_min` freshness
  stamp.
- **Tier 2 (workout debriefs):** `activity_extras()` adds **time-in-HR-zone** (minutes/zone)
  and, for outdoor sessions, **weather** at activity time (skipped when indoor → temp null).
- **Tier 3:** **personal records** (labelled via a best-effort typeId map) in the cached
  `fitness_profile`.
- **Directive:** the COACHING DIRECTIVE now reasons over skin-temp illness flags, the RHR/
  respiration/HRV cluster, stress-duration, sedentary strain, Load Focus (which session type
  to add), sweat-loss rehydration, PR nudges, and per-workout HR-zone/weather.

## Gotchas
- Recovery time is in **minutes** (÷60 for hours). Race predictions are **seconds**.
  Weight in exercise sets is **grams** (÷1000 for kg). Body-battery current =
  `get_stats.bodyBatteryMostRecentValue` (the number the Connect app/web show; day high/low ≠
  current). It reflects the **last watch sync** and can lag the wrist until the watch uploads —
  the API is exactly as fresh as Connect web, never behind it. Snapshot carries
  `body_battery_current_as_of` / `_age_min` for an honest "as of" stamp.
- `get_body_composition().dateWeightList` is ordered **newest-first**; take `dwl[0]` / the
  max `calendarDate` for the latest weigh-in, never `dwl[-1]` (that's the oldest in the window).
- Endurance/hill classification **IDs are 0-indexed**; label from the returned
  `classificationLowerLimit*` thresholds, not a hard-coded map.
- Performance-suite endpoints change slowly → cached ~daily (`FITNESS_MAX_AGE_H`), not
  fetched per message, to keep the bot responsive.
- **Skin temp is a *deviation*** from a rolling ~19-night baseline (`skinTempCalibrationDays`),
  not an absolute temperature — coach on the *change* (±0.5°C is meaningful), and only when it
  corroborates RHR/HRV/respiration. `breathingDisruptionData` uses **255 as a "no reading"
  sentinel** (Connect shows `--`) — count only values `0 < v < 255`.
- **Personal-record `typeId` → label is best-effort** (1–8 running/riding are reliable; 9–16
  steps/streaks inferred). `value` units differ by record: time PRs = seconds, distance PRs =
  metres, step/streak PRs = raw count — formatted accordingly in `_format_prs`.
- `get_user_summary` is the **richest single call** (whole-day movement, stress-duration, RHR
  trend, BB accounting, last-sync). `get_activity_weather` returns **all-null for indoor**
  activities (guard on `temp is not None`); works only on outdoor GPS sessions.
- At/just-after **local midnight**, "today" has no synced data yet → snapshot fields come back
  null (graceful, not a bug); the morning brief runs at 09:30 once the watch has synced.
