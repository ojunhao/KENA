// Run against a local KENA server. Writes only synthetic events; never resets data.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES
  ? process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES + '/playwright' : 'playwright');
const base = process.env.KENA_TEST_URL || 'http://127.0.0.1:8000';

(async () => {
  const loaded = process.env.KENA_CHROMIUM_PACKAGE ? require(process.env.KENA_CHROMIUM_PACKAGE) : null;
  const packaged = loaded?.default || loaded;
  const browser = await chromium.launch({headless:true,
    ...(packaged ? {executablePath:await packaged.executablePath(),args:packaged.args} : {}),
    ...(process.env.KENA_CHROMIUM_PATH ? {executablePath:process.env.KENA_CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage']} : {})});
  let page;
  try {
    page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base);
    await page.getByRole('button',{name:'+ Simulate S$8,000 transfer'}).click();
    await page.getByRole('button',{name:'Not my transfer'}).click();
    await page.getByRole('button',{name:'Confirm pause & open case'}).waitFor({state:'visible'});
    assert.match(await page.locator('.transaction-detail').innerText(), /awaiting confirmation/);
    await page.getByRole('button',{name:'Confirm pause & open case'}).click();
    await page.locator('.case-card').waitFor();
    assert.match(await page.locator('.case-card').innerText(), /Transfer paused · case open/);
    assert.match(await page.locator('#outbox').innerText(), /SES/);
    assert.match(await page.locator('#audit').innerText(), /pause_transfer/);

    await page.locator('#fallback').check();
    await page.getByRole('button',{name:'+ Simulate S$8,000 transfer'}).click();
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
    if(process.env.KENA_SCREENSHOT) await page.screenshot({path:process.env.KENA_SCREENSHOT,fullPage:true});

    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth),false,'Mobile page overflows horizontally');
    assert.deepEqual(errors,[]);
    console.log('PASS: browser protection, SES outbox, audit, SMS fallback, cancellation, XSS escaping, mobile layout; zero JS errors');
  } catch(error) {
    if(process.env.KENA_SCREENSHOT && page) await page.screenshot({path:process.env.KENA_SCREENSHOT,fullPage:true});
    if(page) console.error(await page.evaluate(()=>({detail:document.querySelector('#transaction-detail')?.textContent,error:document.querySelector('#error')?.textContent,proposalHidden:document.querySelector('#proposal')?.hidden,body:document.body.innerText.slice(0,1200)})));
    throw error;
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
