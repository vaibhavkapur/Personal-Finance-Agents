import {test,expect} from '@playwright/test';

test('customer compares, approves, verifies arrival and opens operations',async({page})=>{
 const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('/');
 await page.getByRole('button',{name:'New transfer',exact:true}).click();
 await page.getByRole('button',{name:'Compare providers',exact:true}).click();
 await expect(page.locator('.quote-card')).toHaveCount(3);
 const fast=page.locator('.quote-card').filter({hasText:'SwiftSend'});
 await expect(fast.getByRole('button',{name:'Review quote'})).toBeDisabled();
 await page.getByRole('checkbox',{name:/I confirm Ananya/}).check();
 await fast.getByRole('button',{name:'Review quote'}).click();
 const dialog=page.getByRole('dialog');
 await expect(dialog.getByText('₹41,184.00',{exact:true})).toBeVisible();
 await expect(dialog.getByRole('button',{name:'Approve & send'})).toBeDisabled();
 await dialog.getByRole('checkbox').check();
 await dialog.getByRole('button',{name:'Approve & send'}).click();
 await expect(page.getByRole('heading',{name:'Waiting for source funding'})).toBeVisible({timeout:10000});
 await expect(page.getByText('Download receipt')).toHaveCount(0);
 await page.getByRole('button',{name:'+ 5 minutes',exact:true}).click();
 await expect(page.getByRole('heading',{name:'On its way to the recipient bank'})).toBeVisible();
 await page.getByRole('button',{name:'+ 24 hours',exact:true}).click();
 await expect(page.getByRole('heading',{name:'It’s arrived. And accounted for.'})).toBeVisible();
 await expect(page.getByRole('link',{name:'Download receipt'})).toBeVisible();
 await page.reload();await expect(page.getByRole('heading',{name:'It’s arrived. And accounted for.'})).toBeVisible();
 await page.screenshot({path:'artifacts/delivery.png',fullPage:true});
 await page.getByRole('button',{name:'Operations console'}).click();
 await expect(page.getByRole('heading',{name:'A clear view of the rails.'})).toBeVisible();
 await page.getByRole('button',{name:'Jobs',exact:true}).click();
 await expect(page.locator('.table-row').first()).toBeVisible();
 expect(errors).toEqual([]);
});

test('mobile comparison has no horizontal overflow and can ask the concierge',async({page})=>{
 await page.setViewportSize({width:390,height:844});await page.goto('/');
 await page.getByRole('button',{name:'New transfer',exact:true}).click();
 await page.getByRole('button',{name:'Compare providers',exact:true}).click();
 await expect(page.locator('.quote-card')).toHaveCount(3);
 await page.getByRole('button',{name:'Is arrival guaranteed?'}).click();
 await expect(page.locator('.concierge-answer')).toContainText('not guaranteed');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.screenshot({path:'artifacts/mobile.png',fullPage:true});
});
