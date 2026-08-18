// vite 原生 CSS modules（*.module.css）的 TS 侧声明：
// import css from './X.module.css' 得到 类名 → 编译后类名 的只读映射。
declare module '*.module.css' {
  const classes: { readonly [key: string]: string }
  export default classes
}
