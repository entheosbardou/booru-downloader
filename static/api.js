// Shared JSON transport. Callers own cancellation and user-facing errors.
async function api(path,body,{signal,timeout=30000}={}){
  const controller=new AbortController();
  const abort=()=>controller.abort();
  if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true});
  const timer=setTimeout(abort,timeout);
  try{
    const options={signal:controller.signal};
    if(body!==undefined)Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const response=await fetch('/api'+path,options);
    let data;
    try{data=await response.json()}catch(error){if(error.name==='AbortError')throw error;throw Error('The server returned an unexpected response.')}
    if(!response.ok)throw Error(data.error||`Request failed (${response.status})`);
    return data;
  }catch(error){
    if(error.name==='AbortError'&&!signal?.aborted)throw Error('The request timed out. Please retry.');
    throw error;
  }finally{clearTimeout(timer);signal?.removeEventListener('abort',abort)}
}
