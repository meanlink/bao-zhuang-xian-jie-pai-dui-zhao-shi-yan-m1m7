import {JobStatus,ReviewStatus} from '../../constants/enums.js';
import {esc} from '../../utils/dom.js';
export const status=v=>'<span class="status '+esc(v)+'">'+esc(JobStatus[v]||v)+'</span>';
export const reviewStatus=v=>'<span class="status review '+esc(v)+'">'+esc(ReviewStatus[v]||v)+'</span>';
