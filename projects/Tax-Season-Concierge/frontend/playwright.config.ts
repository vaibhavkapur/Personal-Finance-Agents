import { defineConfig } from '@playwright/test'
export default defineConfig({testDir:'e2e',fullyParallel:false,workers:1,timeout:60000,use:{baseURL:'http://127.0.0.1:8096',headless:true,launchOptions:{executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'},viewport:{width:1440,height:1100}},reporter:'list'})
