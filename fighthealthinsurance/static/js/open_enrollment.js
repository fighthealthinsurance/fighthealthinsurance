// Open enrollment page (templates/open_enrollment.html). Plain script, no build
// step: it draws the marketplace charts and their year tables from the numbers
// the view embeds with json_script (the marketplace entry of
// static/denial_watch.json), with js/denial_watch_charts.js's line chart.
(function(){
const DATA_EL=document.getElementById('dw-data');const ROOT=document.querySelector('.dw');
if(!DATA_EL||!ROOT||!window.DWCharts)return;
const {foldLinks,fmt,pct,tok,line}=window.DWCharts;
/* Before the data is read, so a bad bundle or a chart that throws can't leave a link stuck on a closed fold. */
foldLinks();
const D=JSON.parse(DATA_EL.textContent);
const $=id=>document.getElementById(id);

/* marketplace coverage: every February from one CMS file, in the count colour, the year CMS is still counting drawn hollow */
const MK=D.marketplace;
if(MK){const F=MK.feb,first=F[0].y,lastY=F[F.length-1].y;
  line($('mkline'),F.map(r=>({x:r.y,y:r.n})),{ymin:0,ymax:25e6,ticks:[0,5e6,10e6,15e6,20e6,25e6],fmtY:y=>y?(y/1e6)+'M':'0',color:tok('--dw-count'),label:y=>(y/1e6).toFixed(1)+'M',open:p=>p.x===MK.prelim});
  $('mkline').setAttribute('aria-label',`Line chart of people with marketplace coverage in effect each February, ${first} to ${lastY}${MK.prelim?`. ${MK.prelim} is preliminary`:''}`);
  const chg=i=>{const c=F[i].n/F[i-1].n-1;return (c>0?'+':'')+pct(c);};
  $('mktable').innerHTML=`<caption class="fhi-visually-hidden">People with marketplace coverage in effect each February, nationwide, ${first} to ${lastY}</caption><thead><tr><th scope="col">February</th><th scope="col" class="dw-r">Coverage in effect</th><th scope="col" class="dw-r">Change</th></tr></thead><tbody>`+
    F.map((r,i)=>`<tr><th scope="row">${r.y}${r.y===MK.prelim?' <span class="dw-note">(preliminary)</span>':''}</th><td class="dw-r dw-num">${fmt(r.n)}</td><td class="dw-r dw-num">${i?chg(i):''}</td></tr>`).join('')+'</tbody>';}

/* plans picked: every open enrollment from 2014, CMS's files and, before 2017, HHS ASPE's reports of CMS data, in the count colour; the year table is drawn by the view */
const H=MK&&MK.history;
if(H&&H.length){const first=H[0].y,lastY=H[H.length-1].y;
  line($('pkline'),H.map(r=>({x:r.y,y:r.n})),{ymin:0,ymax:25e6,ticks:[0,5e6,10e6,15e6,20e6,25e6],fmtY:y=>y?(y/1e6)+'M':'0',color:tok('--dw-count'),label:y=>(y/1e6).toFixed(1)+'M'});
  $('pkline').setAttribute('aria-label',`Line chart of people who picked a marketplace plan in each open enrollment, ${first} to ${lastY}`);}

})();
