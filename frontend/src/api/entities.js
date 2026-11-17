import {api,download} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const reviews={
 list:()=>api('/reviews'),
 get:id=>api('/reviews/'+id),
 create:data=>api('/reviews',{method:'POST',body:JSON.stringify(data)}),
 update:(id,data)=>api('/reviews/'+id,{method:'PUT',body:JSON.stringify(data)}),
 lock:(id,data)=>api('/reviews/'+id+'/lock',{method:'POST',body:JSON.stringify(data||{})}),
 resume:id=>api('/reviews/'+id+'/resume',{method:'POST'}),
 approve:id=>api('/reviews/'+id+'/approve',{method:'POST'}),
 publish:id=>api('/reviews/'+id+'/publish',{method:'POST'}),
 brief:id=>download('reviews/'+id+'/brief',null,'review-brief-'+id+'.csv')
};
