# Athlete Profile — YOUR NAME

_This file personalizes the coach. Copy it to `profile.md` and edit it to describe
yourself — goals, the equipment you can train with, and any constraints. The bot
injects this file into every prompt, and you can also ask the bot to update it for
you ("AgBot: add that I tweaked my knee")._

> Copy this file: `cp profile.example.md profile.md` (Windows: `Copy-Item profile.example.md profile.md`).
> `profile.md` is git-ignored so your personal details never get committed.

## Goals
- **Primary:** _e.g. reduce body fat while building lean muscle (body recomposition)._
- **Secondary:** _e.g. progress cardio fitness — build, don't just maintain: raise VO2max
  and move Garmin Training Status toward Productive (not stuck at Maintaining)._
- **Weight tracking:** _e.g. I weigh in every morning under the same conditions (fasted), so my
  `weight_trend_30d` is low-noise — trust the trend as the headline fat-loss progress signal._

## Training availability
- **N days per week** of intentional training.
- _Note anything about how you like to structure the week (e.g. often stack two short
  sessions in a day, prefer mornings, long run on weekends)._

## Equipment / modalities available
_List everything you can realistically train with — the more specific, the better the
programming. Delete what doesn't apply, add what does._

**Cardio machines:**
- _e.g. treadmill, stationary bike, rowing machine, elliptical, stair climber, ski erg_

**Strength:**
- _e.g. adjustable dumbbells 5–25 kg, barbell + plates, cable machine, pull-up bar,
  kettlebells, resistance bands_

**Studio / floor:**
- _e.g. yoga mat, foam roller, bands_

**Outdoor:**
- _e.g. can run/cycle outdoors anytime_

> Programming note for the coach: _optionally spell out how you want sessions built
> around your kit — e.g. "max dumbbell is 25 kg so use tempo / unilateral work for legs;
> use the rower and bike for Zone 2 and intervals."_

## Preferences & style
- _e.g. enjoy mixing cardio + strength; prefer structured sessions with explicit
  sets/reps/intervals over vague advice; hate burpees; etc._

## Constraints / injuries
- _None recorded yet. Add anything ongoing here (e.g. left knee, lower back) and the
  coach will adapt. The bot also tracks injuries/illness you report in chat._

## Current capability & load anchors
_Optional but recommended — tell the coach what you can ACTUALLY do, so it scales
watts / weights / paces to **you** instead of to Garmin's stored (and often stale) numbers._
- _e.g. **Cycling:** my real working FTP is ~180W (Garmin's stored FTP is old / too high) —
  anchor interval watts to that, not the device value._
- _e.g. **Strength:** working sets around <your weights>, always leaving 1–2 reps in reserve._
- _e.g. **Phase:** rebuilding after time off — progress in small steps (~5%), no all-out or
  to-failure efforts; RPE 8 with a little in reserve is my ceiling for now._
- _The coach also learns from what you report you actually did and re-anchors to it._

## Coaching principles (for the bot)
_Optional — how you want the coach to think. Sensible defaults below; edit to taste._
- **Adapt to readiness daily:** when training readiness is LOW or HRV is below baseline,
  prefer Zone 2 cardio, mobility or a lighter day rather than hard intervals.
- **Protect recovery:** flag short sleep, elevated resting HR, or high ACWR (>1.5) and
  dial intensity back.
- Progressive overload for strength; keep nutrition advice sustainable, never crash tactics.
- **Build, don't just maintain (if that's your goal):** on genuinely good-recovery days
  (GREEN, or a train-ready AMBER), deliberately include vigorous aerobic / VO2max intervals
  aimed at whatever Load Focus is under target — that's what moves Garmin Training Status
  toward Productive; easy or strength-only days hold VO2max but won't lift it. Skip it on
  RED / rest days — recovery wins.
- Always end a workout suggestion with a one-line safety note (warm up, stop if pain).
