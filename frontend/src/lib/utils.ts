import { clsx, type ClassValue } from "clsx";

/** 合并 className（Shadcn/ui 标准工具函数） */
export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}
