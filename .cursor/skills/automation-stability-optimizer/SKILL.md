---
name: automation-stability-optimizer
description: Optimizes Python browser automation scripts for stable end-to-end execution, retry handling, popup recovery, crash restart, and diagnostic logging. Use when improving Playwright, Selenium, Web of Science, SCI export, crawler, captcha-assisted, or long-running browser automation scripts that must be tested until one full successful run completes.
---

# Automation Stability Optimizer

## Goal

Act as a senior automation test engineer. Optimize the current automation script until it can complete one full end-to-end run reliably.

Do not stop after fixing a single visible error. Work in a loop:

1. Read the project code, configuration, state files, and recent debug artifacts.
2. Run the script or the smallest realistic full-flow command.
3. Observe the failure, logs, screenshots, and browser state.
4. Make the smallest focused fix.
5. Run again.
6. Repeat until the script completes one full successful run or a real external blocker requires user action.

## Required Behavior

- The script must run the complete business flow.
- Transient failures should recover automatically where possible.
- If recovery is not enough, the script should restart cleanly and resume from persisted progress.
- Popups, dialogs, overlays, confirmation prompts, unexpected tabs, and blocking notices should be detected and closed automatically.
- Key operations must use retries. Default retry count is 3 unless the code already has a clearer project-specific setting.
- Progress must be persisted often enough that reruns do not repeat completed expensive work.
- Secrets must not be printed, committed, or copied into debug logs.

## Retry Pattern

Apply retries around fragile browser and network operations:

- navigation and page load waits
- login and captcha submission
- clicking menu items or export buttons
- filling forms
- waiting for selectors, downloads, or result counts
- batch export steps
- file writes that update progress

Prefer a shared helper when the project already has retry utilities. Otherwise add a small helper with:

- operation name
- max attempts, default 3
- short delay or backoff between attempts
- screenshot and diagnostic capture on final failure
- clear exception propagation after retries are exhausted

## Popup Handling

Add a reusable popup cleanup step and call it before and after fragile operations.

Handle common blockers:

- JavaScript dialogs through browser dialog handlers
- modal close buttons such as `Close`, `Cancel`, `OK`, `我知道了`, `关闭`, `取消`, `确定`
- cookie notices, notification prompts, ads, masks, timeout notices, and session prompts
- extra browser pages or tabs opened by the site

Use robust selectors first: role, text, label, placeholder, title. Keep existing XPath or CSS locators as fallbacks when they are already used by the project.

## Diagnostic Logging

On errors, preserve enough evidence for later debugging:

- timestamp
- current step name
- current URL
- input parameters relevant to the step
- exception type, message, and stack trace
- console logs
- browser page screenshot
- HTML snapshot
- network request and response summary
- download state or progress state when relevant

Store artifacts under an existing debug or logs directory if present. Use filenames that include timestamp, step name, date or batch range when applicable.

Never log:

- passwords
- captcha API keys
- raw cookies
- tokens
- downloaded private data content

## Restart And Resume

For long-running automation:

- Load existing state before starting.
- Mark work as in progress before expensive steps.
- Save progress after every successful batch or business milestone.
- On startup, resume failed or in-progress items from the next missing unit.
- If the browser crashes or a page becomes unusable, close it, create a new context/page, reload state, and continue.
- Avoid infinite restart loops. Track restart count and stop with diagnostics if the same blocker repeats too many times.

## Testing Loop

After each code change:

1. Run the script again.
2. Inspect whether the failure moved forward or repeated.
3. If it repeated, gather more evidence before changing the same area again.
4. Fix only the current confirmed cause.
5. Continue until one complete run succeeds.

If the flow requires manual intervention, such as login approval, SMS code, captcha service failure, missing permission, or account lock, stop and tell the user exactly what to do next. Continue after the user confirms completion.

## Reporting

During work, keep updates concise and evidence based:

- what failed
- what was changed
- what will be tested next

Final response must include:

- completed stability improvements
- how to start the script
- where debug artifacts are saved
- retry and recovery behavior
- final test result
- any remaining external risks
