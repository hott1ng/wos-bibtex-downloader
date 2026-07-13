


from playwright import sync_playwright

def ddddocr识别登录验证码():
    pass


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config.url)
        page.wait_for_load_state("domcontentloaded")
        page.click("text=登录")
        page.fill("input[name='username']", config.username)
        page.fill("input[name='password']", config.password)
        ddddocr识别登录验证码()
        page.click("text=登录")
        page.wait_for_load_state("domcontentloaded")
        page.click("text=下载")
        page.wait_for_load_state("domcontentloaded")
