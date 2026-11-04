// Sankaku failed-post redirect checker
// Run in the booru app's browser console, then select the exported failures JSON.
(async () => {
  const file = await new Promise((resolve, reject) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json,application/json';
    input.onchange = () => input.files?.[0] ? resolve(input.files[0]) : reject(new Error('No file selected.'));
    input.click();
  });
  const data = JSON.parse(await file.text());
  if (!Array.isArray(data)) throw new Error('Expected the exported failures JSON array.');

  const failures = data.filter(job => job.site_api === 'sankaku' && job.post_url);
  console.log(`Checking ${failures.length} Sankaku failure post URLs…`);
  const results = [];
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  for (const [i, job] of failures.entries()) {
    let status = '', finalUrl = '', error = '';
    try {
      const response = await fetch(job.post_url, {
        method: 'GET', redirect: 'follow', credentials: 'include', referrerPolicy: 'no-referrer'
      });
      finalUrl = response.url;
      status = new URL(finalUrl).pathname.toLowerCase().includes('en/posts/show_empty')
        ? 'show_empty redirect' : 'other destination';
      console.log(`${i + 1}/${failures.length} failure #${job.id}: ${status}`, finalUrl);
    } catch (e) {
      status = 'check failed';
      error = String(e);
      console.warn(`${i + 1}/${failures.length} failure #${job.id}: check failed`, error);
    }
    results.push({failure_id: job.id, kind: job.kind, site_name: job.site_name,
      post_id: job.post_id, post_url: job.post_url, result: status, final_url: finalUrl, error});
    // Keep requests to roughly one every 10 seconds, with a 60-second pause every 10 checks.
    if (i < failures.length - 1) await wait((i + 1) % 50 === 0 ? 60000 : 5000);
  }

  const cols = ['failure_id','kind','site_name','post_id','post_url','result','final_url','error'];
  const csv = [cols, ...results.map(row => cols.map(key => row[key] ?? ''))]
    .map(row => row.map(value => `"${String(value).replaceAll('"','""')}"`).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = `sankaku-redirect-check-${new Date().toISOString().slice(0,10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
  console.log(`Finished. Downloaded results for ${results.length} Sankaku entries.`);
})();
