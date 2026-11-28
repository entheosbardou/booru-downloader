// Saved-result navigation owns its in-flight requests independently of Activity.
let postsController=null,libraryLoaded=false,failedBrowseSelection=null;
function browseSelection(){return JSON.stringify([active,page,perPage,resultSort,$('filter').value])}
function shouldRefreshPosts(){return !pageLoading&&browseRevision()!==renderKey&&browseSelection()!==failedBrowseSelection}
function browseRevision(){const found=activeSearch();return found?`${active}:${found.count}:${JSON.stringify(found.children?state.searches.filter(s=>found.children.includes(s.id)).map(s=>s.last_crawl):found.last_crawl)}:${state.settings.default_path}:${$('filter').value?Math.floor((state.tag_index?.updated||0)/30):0}`: ''}
let refreshPending=null;
function refresh({fresh=false}={}){if(refreshPending)return fresh?refreshPending.then(()=>refresh()):refreshPending;refreshPending=refreshLibrary().finally(()=>{refreshPending=null});return refreshPending}
async function refreshLibrary(){try{const library=await api('/library');state={...state,...library,settings:{...state.settings,...library.settings}};libraryLoaded=true;const ti=state.tag_index;$('tag-index-status').textContent=ti&&!ti.ready?`Preparing saved-tag search: ${(ti.completed||0).toLocaleString()} / ${(ti.total||0).toLocaleString()} records indexed. Filtered results will grow as indexing continues.${ti.error?' Indexing paused; retrying automatically.':''}`:'';$('source-name').textContent='Booru collection desk';document.title='Booru · Collection desk';renderLibrary();renderSavedCrawlSites();$('storage-label').textContent=state.settings.default_path||'Not configured · downloads off';if(document.activeElement!==$('default-path')&&$('settings').classList.contains('hidden')){$('default-path').value=state.settings.default_path||'';$('delay').value=state.settings.delay>0?Number((60/state.settings.delay).toPrecision(8)):0}if(active!==null&&!hasSearch(active)){active=null;current=[];selected.clear();renderKey='';$('results-title').textContent='Search results';$('count').textContent='';$('pagination').classList.add('hidden');for(const fmt of ['json','csv'])$('export-'+fmt).classList.add('hidden');renderPosts()}if(active===null&&state.collections?.length){await choose('g-'+state.collections[0].id);return}const found=activeSearch();$('count').textContent=found?`${found.count.toLocaleString()} saved`:'';if(shouldRefreshPosts()){await loadPosts()}setBusyControls()}catch(e){if(!libraryLoaded){$('search-list').replaceChildren(el('p','Could not load saved searches.','muted'));const retry=el('button','Retry loading');retry.onclick=()=>refresh();$('search-list').append(retry);$('storage-label').textContent='Settings unavailable'}toast(e.message,true)}}
async function choose(id){active=id;renderLibrary();page=1;selected.clear();renderKey='';current=[];renderPosts();$('pagination').classList.add('hidden');$('browse-total').textContent='';$('page-label').textContent='';$('page-number').value=1;$('filter').value='';$('results-title').textContent=(typeof id==='string'?state.collections.find(g=>'g-'+g.id===id):state.searches.find(s=>s.id===id))?.display_name||(typeof id==='string'?state.collections.find(g=>'g-'+g.id===id):state.searches.find(s=>s.id===id))?.tags||'Search results';for(const fmt of ['json','csv']){const link=$('export-'+fmt);link.href=`/api${activePath(id)}/export.${fmt}`;link.classList.remove('hidden')}await loadPosts()}
// Each section below loads on its own request: the grid never waits for the total, the status badges, or each other.
let totalKnown=false,totalCache={key:'',data:null};
function showTotal(data){
 totalKnown=true;totalPages=data.total_pages;
 $('browse-total').textContent=`of ${totalPages.toLocaleString()} pages · ${data.total.toLocaleString()} matching results`;
 $('pagination').classList.toggle('hidden',!data.total);
 $('page-label').textContent=`Page ${page} of ${totalPages.toLocaleString()}`;$('page-number').max=totalPages;
 $('next').disabled=page>=totalPages;
}
async function loadTotal({base,query,key,signal,superseded}){
 let data=totalCache.key===key?totalCache.data:null;
 if(!data){
  $('browse-total').textContent='Counting matching results…';
  try{data=await api(`${base}/count?${query}`,undefined,{signal,timeout:120000})}
  catch(e){if(signal.aborted||superseded())return;$('browse-total').textContent='Total unavailable. Results above are still usable.';return}
  if(superseded())return;
  totalCache={key,data};
 }
 if(superseded())return;
 showTotal(data);
 // The page request cannot know where the results end; correct an out-of-range page once the total arrives.
 if(data.total&&page>data.total_pages){page=data.total_pages;$('page-number').value=page;loadPosts().catch(e=>toast(e.message,true))}
}
async function loadPosts({scrollTop=false}={}){
 if(active===null)return;
 const requestId=++postsRequest,sid=active,size=perPage,filterValue=$('filter').value,sort=resultSort;
 const term=filterValue.trim();
 const selection=browseSelection();
 failedBrowseSelection=null;
 postsController?.abort();
 const controller=postsController=new AbortController();
 const revision=browseRevision();
 const superseded=()=>requestId!==postsRequest||sid!==active||size!==perPage||sort!==resultSort||filterValue!==$('filter').value||!hasSearch(sid);
 const base=activePath(sid)+'/posts',query=`per_page=${size}&filter=${encodeURIComponent(term)}`;
 let offset=0,failed=false;
 const loaded=new Map();pageLoading=true;$('page-loading').textContent='Loading results…';setBusyControls();
 totalKnown=false;
 $('browse-total').textContent='';
 $('page-number').max='';
 const totalKey=[sid,query,revision].join('|');
 const fetchTotal=()=>loadTotal({base,query,key:totalKey,signal:controller.signal,superseded});
 const tagFilter=term&&!/^(id|job):/i.test(term);
 if(!tagFilter)fetchTotal().catch(()=>{});
 try{
  do{
   const data=await api(`${base}/page?page=${page}&${query}&sort=${encodeURIComponent(sort)}&chunk_size=${term?size:Math.min(size,48)}&chunk_offset=${offset}`,undefined,{signal:controller.signal});
   if(superseded())return;
   renderKey=revision;
   for(const post of data.posts)loaded.set(post.record_id,post);
   current=[...loaded.values()];
   renderPosts();
   $('page-number').value=page;$('previous').disabled=page<=1;
   if(totalKnown)showTotal(totalCache.data);
   else{$('page-label').textContent=`Page ${page}`;$('next').disabled=!data.has_next&&!data.more_on_page;if(current.length||page>1)$('pagination').classList.remove('hidden')}
   $('page-loading').textContent=data.more_on_page?`Loading results… ${current.length}`:'';
   setBusyControls();if(scrollTop&&offset===0)window.scrollTo({top:0,behavior:'smooth'});
   refreshVisiblePostStatus().catch(()=>{});
   // Give the page query priority over the more expensive exact tag count.
   if(tagFilter&&offset===0)fetchTotal().catch(()=>{});
   offset+=data.posts.length;if(!data.more_on_page||!data.posts.length)break;
  }while(offset<size);
 }catch(e){if(controller.signal.aborted)return;failed=true;if(!superseded()){failedBrowseSelection=selection;$('page-loading').textContent='Loading interrupted · use Go to retry.'}throw e}
 finally{if(requestId===postsRequest){pageLoading=false;if(!failed)$('page-loading').textContent='';setBusyControls()}}
}


async function refreshVisiblePostStatus(){if(active===null||!current.length)return;const sid=active,requestId=postsRequest;const data=await api(`${activePath(sid)}/posts/status`,{ids:current.map(p=>p.record_id)});if(sid!==active||requestId!==postsRequest)return;const statuses=new Map(data.posts.map(p=>[p.record_id,p]));for(const post of current){post.status_pending=false;const status=statuses.get(post.record_id);if(status){post.downloaded=status.downloaded;post.queued=status.queued}}renderPosts();setBusyControls()}
