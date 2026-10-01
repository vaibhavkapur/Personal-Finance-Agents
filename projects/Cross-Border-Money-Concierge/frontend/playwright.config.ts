import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'./e2e',workers:1,use:{baseURL:'http://127.0.0.1:8088',headless:true,launchOptions:process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{},trace:'retain-on-failure'},reporter:'list'});
