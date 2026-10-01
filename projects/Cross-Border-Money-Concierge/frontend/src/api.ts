export async function api<T>(path:string, body?:unknown, token?:string, key?:string):Promise<T> {
  const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',...(token?{Authorization:`Bearer ${token}`} : {}),...(key?{'Idempotency-Key':key}: {})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  const data=await response.json();
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'Please check your transfer details and try again.');
  return data;
}
export const money=(minor:number,currency='USD')=>new Intl.NumberFormat(currency==='INR'?'en-IN':'en-US',{style:'currency',currency,minimumFractionDigits:2}).format(minor/100);
export const date=(value:string)=>new Date(value).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
export const label=(value:string)=>value.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
