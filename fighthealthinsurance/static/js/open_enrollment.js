// Open enrollment page (templates/open_enrollment.html). Plain script, no build
// step: it draws the marketplace charts and the February table from the numbers
// the view embeds with json_script (the marketplace and population entries of
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

/* coverage and people under 65: each February's change since the first, against the people under 65 on the July 1 before it, dashed in the darker count colour; the label and year table are drawn by the view */
const U=D.population&&new Map(D.population.under_65.map(r=>[r.y,r.n]));
if(MK&&U&&U.has(MK.feb[0].y-1)){const F=MK.feb.filter(r=>U.has(r.y-1)),f0=F[0].n,u0=U.get(F[0].y-1);
  const chg=y=>(y<0?'':'+')+Math.round(y*100)+'%';
  line($('popline'),F.map(r=>({x:r.y,y:r.n/f0-1})),{ymin:0,ymax:1.25,ticks:[0,0.25,0.5,0.75,1,1.25],fmtY:y=>y?chg(y):'0',color:tok('--dw-count'),label:chg,open:p=>p.x===MK.prelim,
    also:[{pts:F.map(r=>({x:r.y,y:U.get(r.y-1)/u0-1})),color:tok('--dw-count-deep'),label:y=>'Under 65 '+chg(y),dash:'8 5'}]});}

})();
