// Run against an isolated KENA verification server. Creates and deletes synthetic transfers.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES
  ? process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES + '/playwright' : 'playwright');
const base = process.env.KENA_TEST_URL || 'http://127.0.0.1:8000';

(async () => {
  const loaded = process.env.KENA_CHROMIUM_PACKAGE ? require(process.env.KENA_CHROMIUM_PACKAGE) : null;
  const packaged = loaded?.default || loaded;
  const browser = await chromium.launch({headless:true,
    ...(packaged ? {executablePath:await packaged.executablePath(),args:packaged.args} : {}),
    ...(process.env.KENA_CHROMIUM_PATH ? {executablePath:process.env.KENA_CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--no-zygote','--single-process']} : {})});
  let page;
  try {
    page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base);
    const initialCount=await page.locator('.queue-row').count();
    await page.getByRole('textbox',{name:'Transfer amount in SGD'}).fill('0');
    await page.getByRole('button',{name:'Create transfer',exact:true}).click();
    await page.locator('#error').waitFor({state:'visible'});
    assert.match(await page.locator('#error').innerText(),/Amount must be between/);
    assert.equal(await page.locator('.queue-row').count(),initialCount);
    await page.getByRole('textbox',{name:'Transfer amount in SGD'}).fill('8123.45');
    await page.locator('#active-call').check();
    await page.getByRole('button',{name:'Create transfer',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('risk').textContent.includes('90'));
    assert.match(await page.locator('#transaction-detail').innerText(),/S\$8,123.45/);
    assert.match(await page.locator('#chat').innerText(),/someone on the call instructing/);
    assert.equal(await page.locator('h1').count(),0);
    await page.getByRole('button',{name:'Not my transfer'}).click();
    await page.getByRole('button',{name:'Confirm pause & open case'}).waitFor({state:'visible'});
    assert.match(await page.locator('.transaction-detail').innerText(), /awaiting confirmation/);
    await page.getByRole('button',{name:'Confirm pause & open case'}).click();
    await page.locator('.case-card').waitFor();
    assert.match(await page.locator('.case-card').innerText(), /Transfer paused · case open/);
    assert.match(await page.locator('#outbox').innerText(), /SES/);
    assert.match(await page.locator('#audit').innerText(), /pause_transfer/);
    assert.doesNotMatch(await page.locator('body').innerText(), /demo|simulat|synthetic|mock|sandbox/i);
    assert.match(await page.locator('#outbox').innerText(), /prepared/);

    await page.locator('#fallback').check();
    await page.getByRole('button',{name:'Create transfer',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('channel').textContent.startsWith('SMS'));
    await page.getByRole('button',{name:'Not my transfer'}).click();
    await page.getByRole('button',{name:'Confirm pause & open case'}).waitFor({state:'visible'});
    await page.getByRole('button',{name:'Cancel',exact:true}).click();
    await page.waitForFunction(()=>document.getElementById('transaction-detail').textContent.includes('human review'));
    assert.equal(await page.locator('#proposal').isVisible(),false);

    await page.locator('#reply').fill('<img src=x onerror="window.xss=true">');
    await page.getByRole('button',{name:'Send reply'}).click();
    await page.waitForFunction(()=>document.getElementById('chat').textContent.includes('<img src=x'));
    assert.equal(await page.evaluate(()=>window.xss),undefined);
    assert.equal(await page.locator('#chat img').count(),0);
    assert.equal(await page.locator('#error').isVisible(),false);
    const countBefore=await page.locator('.queue-row').count();
    await page.locator('.queue-row.selected [data-delete]').click();
    assert.doesNotMatch(await page.locator('#delete-dialog').innerText(), /demo|simulat|synthetic|mock|sandbox/i);
    await page.locator('#delete-cancel').click();
    assert.equal(await page.locator('.queue-row').count(),countBefore);
    await page.locator('.queue-row.selected [data-delete]').click();
    await page.locator('#delete-confirm').click();
    await page.waitForFunction(count=>document.querySelectorAll('.queue-row').length===count-1,countBefore);
    assert.equal(await page.locator('#error').isVisible(),false);
    if(process.env.KENA_SCREENSHOT) await page.screenshot({path:process.env.KENA_SCREENSHOT,fullPage:true});

    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false,'Mobile page overflows horizontally');
    if(process.env.KENA_MOBILE_SCREENSHOT)await page.screenshot({path:process.env.KENA_MOBILE_SCREENSHOT,fullPage:true});
    // Delete only records created by this browser test; retain unrelated existing journeys.
    await page.locator('.queue-row.selected [data-delete]').click();
    await page.locator('#delete-confirm').click();
    await page.waitForFunction(count=>document.querySelectorAll('.queue-row').length===count-2,countBefore);
    // Delete the CLI harness record only when using the isolated verification mode.
    if(process.env.KENA_ISOLATED_VERIFICATION==='true'){
      while(await page.locator('[data-delete]').count()){
        await page.locator('[data-delete]').first().click();
        const rows=await page.locator('.queue-row').count();
        await page.locator('#delete-confirm').click();
        await page.waitForFunction(n=>document.querySelectorAll('.queue-row').length===n-1,rows);
      }
      assert.match(await page.locator('#chat').innerText(),/No messages/);
      assert.equal(await page.locator('#send').isDisabled(),true);
      assert.match(await page.locator('#value').innerText(),/S\$0.00/);
    }
    assert.deepEqual(errors,[]);
    console.log('PASS: editable cents, active-call outreach, protection, SES, SMS fallback, cancellation, XSS escaping, delete/cancel/reset, mobile layout; zero JS errors');
  } catch(error) {
    if(process.env.KENA_SCREENSHOT && page) await page.screenshot({path:process.env.KENA_SCREENSHOT,fullPage:true});
    if(page) console.error(await page.evaluate(()=>({detail:document.querySelector('#transaction-detail')?.textContent,error:document.querySelector('#error')?.textContent,proposalHidden:document.querySelector('#proposal')?.hidden,body:document.body.innerText.slice(0,1200)})));
    throw error;
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
