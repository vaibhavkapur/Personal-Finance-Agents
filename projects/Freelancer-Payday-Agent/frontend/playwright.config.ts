import { defineConfig } from '@playwright/test';
export default defineConfig({
 testDir:'./e2e',workers:1,fullyParallel:false,timeout:45000,
 use:{baseURL:'http://127.0.0.1:8013',viewport:{width:1440,height:1120},headless:true,launchOptions:{executablePath:process.env.PLAYWRIGHT_CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'},trace:'retain-on-failure'},
 webServer:{command:'PAYDAY_DATA_DIR=$(mktemp -d /private/tmp/payday-browser.XXXXXX) PAYDAY_ORIGINS=http://127.0.0.1:8013 ../.venv/bin/python -m uvicorn backend.app.main:app --app-dir .. --host 127.0.0.1 --port 8013',url:'http://127.0.0.1:8013/health',reuseExistingServer:false,timeout:30000},
 reporter:[['list'],['json',{outputFile:'test-results/report.json'}]]
});
