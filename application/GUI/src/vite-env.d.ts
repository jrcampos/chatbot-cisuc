/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_BUILD_SHA: string;
  readonly VITE_DEPLOY_ENV: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}