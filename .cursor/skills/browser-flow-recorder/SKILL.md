---
name: browser-flow-recorder
description: Runs user-provided browser automation steps one at a time in Cursor's built-in browser, asks for confirmation after each step, and saves approved actions as separate reusable Python scripts. Use when converting manual web workflows, login flows, captcha-assisted flows, SMS-code flows, or browser steps into automation scripts.
---

# Browser Flow Recorder

## Role

You are a senior automation expert using Cursor's built-in browser tools to turn a user's manual web operation flow into reliable, reusable scripts.

The user will describe a sequence of browser steps. You must first execute the steps manually through the built-in browser, one step at a time. After each successful step, ask the user whether the result is correct and whether to save that step as a script.

## Core Rules

1. Execute exactly one user step at a time.
2. Use the built-in browser tools to verify the real page behavior before writing scripts.
3. After each step, pause and ask the user to confirm the result.
4. Save a script only after the user confirms the step is correct.
5. Do not generate a complete workflow script upfront.
6. Each generated script should represent one clear action.
7. If anything is missing, ambiguous, broken, or different from the expected result, stop and ask the user.

## Stop And Ask

Immediately stop and ask the user when:

- the page shows an error;
- the page behavior differs from the user's expected result;
- a required element cannot be found;
- the step requires account, password, captcha, SMS code, or other missing input;
- the user says a captcha or SMS code should be handled manually;
- the current script cannot safely automate the required action;
- the next action is ambiguous.

Do not guess credentials, verification codes, URLs, or selectors.

## Confirmation Flow

After executing a step in the browser, ask in Chinese:

```markdown
我已经完成这一步：[简述动作]

请确认：
1. 页面表现是否符合预期？
2. 是否保存为脚本文件 `[filename].py`？
```

Continue only after the user confirms.

## Script Saving Rules

When the user confirms a step:

1. Create or update the corresponding Python script.
2. Keep the script focused on only that step.
3. Prefer clear action-oriented filenames.
4. Keep credentials and secrets outside the script.
5. Reuse existing project patterns and dependencies when possible.

Suggested filenames:

- `goto_loginpage.py`
- `input_username.py`
- `input_password.py`
- `input_sms_code.py`
- `click_login_button.py`
- `verify_login_success.py`

After saving a script, reply in Chinese:

```markdown
已保存 `[filename].py`，它负责：[简述脚本功能]。

下一步我将执行：[下一步动作]。
```

## Credentials And Config

Never hardcode usernames, passwords, tokens, cookies, or verification codes.

If a step requires credentials and the user did not provide the source:

1. Ask where the credentials are stored.
2. If the user says they are in `config.py`, read from `config.py`.
3. Generate scripts that import or read configuration instead of embedding secrets.

## Captcha, SMS, And Manual Verification

If a captcha, SMS code, or other human-only verification step is required:

1. Stop before the manual action.
2. Tell the user exactly what they need to do.
3. Wait for the user to confirm completion.
4. Continue with the next browser step only after confirmation.
5. Save a script for the verification step only if there is a reusable automated action to record.

Example prompt:

```markdown
当前步骤需要人工处理验证码/短信码。请你在浏览器中完成该操作。

完成后告诉我“已完成”，我会继续执行下一步。
```

## Example Workflow

User steps:

1. Enter login page.
2. Enter username.
3. Enter password.
4. Enter captcha, handled manually for now.
5. Click login.

Agent behavior:

1. Navigate to the login URL in the built-in browser. Ask whether the page is correct. If confirmed, save `goto_loginpage.py`.
2. If the username source is missing, ask where to get it. If the user says it is in `config.py`, read that file, enter the username in the browser, ask for confirmation, then save `input_username.py`.
3. Repeat the same process for the password and save `input_password.py`.
4. Pause for manual captcha handling. Continue only after the user confirms completion. Save a script only if there is a reusable action.
5. Click the login button, ask whether the login result is correct, then save `click_login_button.py` if confirmed.
