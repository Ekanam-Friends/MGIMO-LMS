/* Вспомогательный модуль для CUA REPL. Все действия браузера выполняются через
 * документированные API переданных Tab/Browser. HTTP-клиента и чтения cookie нет.
 * Ссылки берутся только из уже наблюдавшегося каталога и сверяются с текущим DOM.
 */
import fs from 'node:fs/promises';
import crypto from 'node:crypto';

export async function createLoader(root, browser, listTabs) {
  const all = JSON.parse(await fs.readFile(`${root}/catalog-all-links.json`, 'utf8'));
  const documents = new Map();
  const deferred = new Set();
  const failures = await fs.readFile(`${root}/download-failures.jsonl`,'utf8').catch(()=> '');
  for (const line of failures.split('\n').filter(Boolean)) deferred.add(JSON.parse(line).url);
  for (const row of all) {
    if (!row.selection_status.startsWith('excluded_') && !documents.has(row.url)) documents.set(row.url, row);
  }
  const saved = async url => {
    const filename = new URL(url).pathname.split('/').pop();
    const directory = filename.endsWith('.docx') ? 'regulations-originals' : 'regulations';
    return fs.stat(`${root}/${directory}/${filename}`).then(() => true).catch(() => false);
  };
  async function save(asset) {
    const item = documents.get(asset.url);
    if (!item) throw new Error('Ресурс вне согласованного перечня');
    const data = await fs.readFile(asset.path);
    if (data.subarray(0, 5).toString() !== '%PDF-') throw new Error('Ресурс не PDF');
    const filename = new URL(asset.url).pathname.split('/').pop();
    const record = {...item, filename, bytes:data.length, downloaded_at:new Date().toISOString(),
      sha256:crypto.createHash('sha256').update(data).digest('hex')};
    await fs.writeFile(`${root}/regulations/${filename}.pending`, data);
    await fs.rename(`${root}/regulations/${filename}.pending`,`${root}/regulations/${filename}`);
    await fs.appendFile(`${root}/download-journal.jsonl`, JSON.stringify(record) + '\n');
    return {filename, bytes:data.length};
  }
  async function drain(max=6) {
    const tabs = await listTabs();
    const pending = [];
    const seen = new Set();
    for (const info of tabs) {
      if (documents.has(info.url) && !seen.has(info.url) && !deferred.has(info.url) && !await saved(info.url)) {
        pending.push(info);seen.add(info.url);
      }
      if (pending.length >= max) break;
    }
    const report = [];
    for (let offset=0; offset<pending.length; offset+=3) {
      const results = await Promise.allSettled(pending.slice(offset,offset+3).map(async info => {
        const tab = await browser.tabs.get(info.id);
        const assets = await tab.capabilities.get('pageAssets');
        const inventory = await assets.list();
        const pdf = inventory.assets.find(item => item.kind === 'pdf' && item.url === info.url);
        if (!pdf) return {url:info.url, error:'PDF ещё не готов или сервер вернул другой формат'};
        const bundle = await assets.bundle({inventoryId:inventory.id, assetIds:[pdf.id]});
        if (!bundle.assets.length) {
          deferred.add(info.url);
          const failure={url:info.url,failed_at:new Date().toISOString(),failures:bundle.failures};
          await fs.appendFile(`${root}/download-failures.jsonl`,JSON.stringify(failure)+'\n');
          return failure;
        }
        return {...await save(bundle.assets[0]), tab};
      }));
      for (const result of results) {
        if (result.status === 'fulfilled') {
          const {tab,...record} = result.value;
          report.push(record);
          if (tab) await tab.close();
        } else report.push({error:String(result.reason)});
      }
    }
    return report;
  }
  async function open(tab, source, max=6) {
    const anchors = await tab.playwright.evaluate(() => Array.from(document.querySelectorAll('a[href]'))
      .map(a => ({url:a.href, href:a.getAttribute('href')})));
    const existing = await listTabs();
    const queued = [];
    for (const anchor of anchors) {
      const item = documents.get(anchor.url);
      if (!item || deferred.has(anchor.url) || await saved(anchor.url) || queued.includes(anchor.url)) continue;
      if (!item.source_page || source !== await tab.url()) throw new Error('Открыт другой раздел каталога');
      if (!existing.some(info => info.url === anchor.url)) {
        try {await tab.playwright.locator(`a[href=${JSON.stringify(anchor.href)}]`).first().click();}
        catch (_) { /* ниже проверяется фактическое появление вкладки */ }
      }
      queued.push(anchor.url);
      if (queued.length >= max) break;
    }
    const after = await listTabs();
    const last = after.find(info => queued.includes(info.url));
    if (last) await (await browser.tabs.get(last.id)).getAXState({emit:false});
    else await tab.getAXState({emit:false});
    return {queued:queued.length, opened:after.filter(info => queued.includes(info.url)).length};
  }
  return {open,drain,retry:url=>deferred.delete(url)};
}
