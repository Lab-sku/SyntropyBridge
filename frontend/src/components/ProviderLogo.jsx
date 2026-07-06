import React, { useEffect, useState } from 'react';
import { cn } from '@/lib/utils';

/**
 * Brand-coloured SVG logos for every provider we surface in the UI.
 *
 * Resolution order for a given ``provider`` id:
 *
 *   1. ``/providers/{id}.svg`` — real, brand-accurate logo that we
 *      mirror from `simple-icons` into ``frontend/public/providers``.
 *      These are the highest fidelity render.
 *   2. ``PATHS[provider]`` — the hand-rolled inline SVG in this file.
 *      Used for vendors that don't ship on `simple-icons` (e.g. Zhipu,
 *      vLLM, iFlytek Spark).
 *   3. Coloured initial chip — last-resort fallback so the picker
 *      still looks consistent if neither source is registered.
 *
 * Conventions:
 *
 *   - viewBox is always `0 0 24 24` so consumers can size with a
 *     single `size` prop.
 *   - inline logos declare a `currentColor` or explicit fill so
 *     dark-mode doesn't invert a vendor's brand mark.
 *   - real logos are loaded as `<img>` so the browser can cache
 *     them across pages.
 */
const size = (n) => ({ width: n, height: n, viewBox: '0 0 24 24' });

// List of providers that have a real /providers/{id}.svg file
// shipped under frontend/public/providers. Anything outside this
// set will fall through to the inline PATHS table below.
const REMOTE_LOGOS = new Set([
  'openai',
  'anthropic',
  'google',
  'deepseek',
  'moonshot',
  'kimi',
  'ollama',
  'mistral',
  'nvidia',
  'openrouter',
  'aliyun',
  'doubao',
  'mimo',
  'hunyuan',
  'wenxin',
  'baichuan',
]);

const PATHS = {
  // OpenAI — the iconic 6-petal knot
  openai: (s) => (
    <svg {...size(s)} fill="none" aria-hidden="true">
      <path
        d="M22.282 9.821a5.985 5.985 0 0 0-.516-4.91 6.046 6.046 0 0 0-6.51-2.9A6.065 6.065 0 0 0 4.981 4.18a5.985 5.985 0 0 0-3.998 2.9 6.046 6.046 0 0 0 .743 7.097 5.98 5.98 0 0 0 .51 4.911 6.052 6.052 0 0 0 6.515 2.9A5.985 5.985 0 0 0 13.26 24a6.056 6.056 0 0 0 5.772-4.206 5.99 5.99 0 0 0 3.997-2.9 6.056 6.056 0 0 0-.747-7.073zM13.26 22.43a4.476 4.476 0 0 1-2.876-1.04l.141-.081 4.779-2.758a.795.795 0 0 0 .392-.681v-6.737l2.02 1.168a.071.071 0 0 1 .038.052v5.583a4.504 4.504 0 0 1-4.494 4.494zM3.6 18.304a4.47 4.47 0 0 1-.535-3.014l.142.085 4.783 2.759a.771.771 0 0 0 .78 0l5.843-3.369v2.332a.08.08 0 0 1-.033.062L9.74 19.95a4.5 4.5 0 0 1-6.14-1.646zM2.34 7.896a4.485 4.485 0 0 1 2.366-1.973V11.6a.766.766 0 0 0 .388.676l5.815 3.355-2.02 1.168a.076.076 0 0 1-.071 0l-4.83-2.786A4.504 4.504 0 0 1 2.34 7.872zm16.597 3.855l-5.833-3.387L15.119 7.2a.076.076 0 0 1 .071 0l4.83 2.787a4.5 4.5 0 0 1-.676 8.105V12.43a.79.79 0 0 0-.407-.685zm2.01-3.023l-.141-.085-4.774-2.782a.776.776 0 0 0-.785 0L9.409 9.23V6.897a.066.066 0 0 1 .028-.061l4.83-2.787a4.5 4.5 0 0 1 6.68 4.66zM9.776 14.628l-2.02-1.164a.08.08 0 0 1-.038-.057V7.829a4.5 4.5 0 0 1 7.375-3.453l-.142.08-4.778 2.758a.795.795 0 0 0-.393.681zm1.097-2.365l2.602-1.5 2.607 1.5v2.999l-2.597 1.5-2.607-1.5z"
        fill="#10A37F"
      />
    </svg>
  ),
  // Anthropic — the "A" mark
  anthropic: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M8.5 2L2 22h3.25l1.5-4.6h6.5L14.75 22H18L11.5 2H8.5zm.85 5.55L11.55 14H7.65l1.7-6.45z"
        fill="#D97757"
      />
    </svg>
  ),
  // Google — 4-color G
  google: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M22.5 12.27c0-.78-.07-1.53-.2-2.27H12v4.3h5.92a5.06 5.06 0 0 1-2.2 3.32v2.76h3.55c2.08-1.92 3.28-4.74 3.28-8.11z"
        fill="#4285F4"
      />
      <path
        d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.55-2.76c-.99.66-2.24 1.06-3.73 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23z"
        fill="#34A853"
      />
      <path
        d="M5.84 14.11A6.6 6.6 0 0 1 5.5 12c0-.73.13-1.44.34-2.11V7.05H2.18A11 11 0 0 0 1 12c0 1.78.43 3.46 1.18 4.95l3.66-2.84z"
        fill="#FBBC05"
      />
      <path
        d="M12 5.36c1.62 0 3.07.56 4.21 1.65l3.15-3.15C17.45 2.08 14.97 1 12 1A11 11 0 0 0 2.18 7.05l3.66 2.84C6.71 7.29 9.14 5.36 12 5.36z"
        fill="#EA4335"
      />
    </svg>
  ),
  // DeepSeek — whale logo, dark navy
  deepseek: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="11" fill="#1E40AF" />
      <path
        d="M5 13.5c1.5 1.8 3.7 2.5 6 2.5 2.3 0 4.5-.7 6-2.5M7 9.5h.01M17 9.5h.01M8 16.5c1 .8 2.5 1.2 4 1.2s3-.4 4-1.2"
        stroke="#fff"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  ),
  // Moonshot / Kimi — crescent moon
  moonshot: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="11" fill="#7C3AED" />
      <path
        d="M16.5 12.3a4.7 4.7 0 0 1-4.8-4.7c0-1 .3-1.9.9-2.6a5 5 0 1 0 6.6 6.6c-.7.5-1.6.7-2.7.7z"
        fill="#fff"
      />
    </svg>
  ),
  // Zhipu GLM — red
  zhipu: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#E11D48" />
      <path
        d="M7 17V7h4.2c2.1 0 3.5 1.1 3.5 2.9 0 1.4-.8 2.2-1.9 2.5l2.4 4.6h-2.6l-2.1-4.2H9V17H7zm2-6.2h2c1 0 1.6-.5 1.6-1.4S12 8 11 8H9v2.8z"
        fill="#fff"
      />
    </svg>
  ),
  // Aliyun Qwen — orange
  aliyun: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#F59E0B" />
      <path
        d="M8 7v10M12 7c2 0 3 1.5 3 3s-1 3-3 3h-1V7h1zM16 13c1 1 2 2 2 4"
        stroke="#fff"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  ),
  // Doubao (字节) — teal/blue
  doubao: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" fill="#3B82F6" />
      <circle cx="9" cy="11" r="1.2" fill="#fff" />
      <circle cx="15" cy="11" r="1.2" fill="#fff" />
      <path
        d="M9 14.5c.8.8 1.9 1.2 3 1.2s2.2-.4 3-1.2"
        stroke="#fff"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  ),
  // OpenRouter — pink/purple
  openrouter: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#EC4899" />
      <path
        d="M7 9h7a3 3 0 0 1 3 3v0a3 3 0 0 1-3 3H7M7 9l3-3M7 9l3 3M7 15l-3 3M7 15l-3-3"
        stroke="#fff"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  // NVIDIA NIM — green
  nvidia: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="3" fill="#76B900" />
      <path
        d="M5 8.5c2 0 3 .5 4 1.5M5 12c3 0 5 1 7 2.5M5 15.5c4 0 7 1.5 9 3M9 7v10.5M12 8.5V17M15 10v6.5M18 11.5V15"
        stroke="#fff"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  ),
  // MiniMax — black
  minimax: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#0F172A" />
      <path
        d="M8 7l-3 5 3 5M16 7l3 5-3 5M14 7l-4 10"
        stroke="#fff"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  // Ollama — teal
  ollama: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" fill="#0D9488" />
      <circle cx="9" cy="11" r="1.2" fill="#fff" />
      <circle cx="15" cy="11" r="1.2" fill="#fff" />
      <circle cx="9" cy="15" r="1" fill="#fff" />
      <circle cx="15" cy="15" r="1" fill="#fff" />
    </svg>
  ),
  // vLLM — cyan
  vllm: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#06B6D4" />
      <text
        x="12"
        y="15"
        textAnchor="middle"
        fontSize="9"
        fontWeight="700"
        fill="#fff"
        fontFamily="ui-sans-serif, system-ui, sans-serif"
      >
        v
      </text>
    </svg>
  ),
  // LM Studio — purple
  lmstudio: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#A855F7" />
      <text
        x="12"
        y="15"
        textAnchor="middle"
        fontSize="8"
        fontWeight="700"
        fill="#fff"
        fontFamily="ui-sans-serif, system-ui, sans-serif"
      >
        LM
      </text>
    </svg>
  ),

  // 硅基流动 SiliconFlow — purple/blue
  siliconflow: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="sf-g" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
          <stop stopColor="#6366F1" />
          <stop offset="1" stopColor="#A855F7" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="20" height="20" rx="5" fill="url(#sf-g)" />
      <path d="M6 9h12M6 12h12M6 15h8" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="17" cy="15" r="1.2" fill="#fff" />
    </svg>
  ),
  // 阶跃星辰 StepFun
  stepfun: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#0EA5E9" />
      <path
        d="M7 17V7l10 10V7"
        stroke="#fff"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  // 书生 InternLM (Shanghai AI Lab) — red
  internlm: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#DC2626" />
      <path d="M7 8h10M7 12h10M7 16h6" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  ),
  // 月之暗面 Moonshot-AI / Kimi 备用
  kimi: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="10" fill="#000" />
      <path
        d="M14 7.5c-2.8 0-5 2.2-5 4.9 0 1.3.5 2.5 1.4 3.4M10 16.5c2.8 0 5-2.2 5-4.9 0-1.3-.5-2.5-1.4-3.4"
        stroke="#fff"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  ),
  // 小米 MiMo
  mimo: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#FF6900" />
      <path d="M8 6h8v2H8zM7 9h10v2H7zM7 12h10v6H7z" fill="#fff" />
    </svg>
  ),
  // Coze
  coze: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="6" fill="#0066FF" />
      <circle cx="9" cy="12" r="2.5" fill="#fff" />
      <circle cx="15" cy="12" r="2.5" fill="#fff" />
    </svg>
  ),
  // 百川 Baichuan
  baichuan: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#0EA5E9" />
      <path d="M6 17V7h2.5l5 7V7H16v10h-2.5l-5-7v7H6z" fill="#fff" />
    </svg>
  ),
  // 文心 Wenxin
  wenxin: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#1D4ED8" />
      <path d="M12 6l4 6-4 6-4-6 4-6z" fill="#fff" />
    </svg>
  ),
  // 讯飞星火 Spark
  spark: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#7C3AED" />
      <path d="M12 6l1.6 4.4L18 12l-4.4 1.6L12 18l-1.6-4.4L6 12l4.4-1.6L12 6z" fill="#fff" />
    </svg>
  ),
  // 腾讯混元 Hunyuan
  hunyuan: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#0F766E" />
      <path
        d="M8 7l4 10 4-10M10 12h4"
        stroke="#fff"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  // 百炼 Bailian (阿里云百炼)
  bailian: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="bl-g" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
          <stop stopColor="#FF6A00" />
          <stop offset="1" stopColor="#FF3D7F" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="20" height="20" rx="5" fill="url(#bl-g)" />
      <path
        d="M8 17V7h4l4 10V7"
        stroke="#fff"
        strokeWidth="1.6"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  ),
  // 360 智脑
  zhinao: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#00B86B" />
      <text
        x="12"
        y="15"
        textAnchor="middle"
        fontSize="9"
        fontWeight="700"
        fill="#fff"
        fontFamily="ui-sans-serif, system-ui, sans-serif"
      >
        360
      </text>
    </svg>
  ),
  // 商汤日日新 SenseChat
  sensenova: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#FF5E1A" />
      <circle cx="12" cy="12" r="5" stroke="#fff" strokeWidth="1.6" fill="none" />
      <circle cx="12" cy="12" r="1.5" fill="#fff" />
    </svg>
  ),
  // MiniMax (additional brand)
  yi: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#0EA5E9" />
      <path d="M7 7l4 5 4-5v10l-4-5-4 5V7z" fill="#fff" />
    </svg>
  ),
  // Groq
  groq: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#F55036" />
      <path d="M7 8v8M11 8v8M15 8v8" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  ),
  // Mistral
  mistral: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#000" />
      <path
        d="M7 7l-2 5 2 5M17 7l2 5-2 5M14 7l-4 10"
        stroke="#FFB800"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  // Cohere
  cohere: (s) => (
    <svg {...size(s)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="5" fill="#39594D" />
      <path
        d="M7 12c0-2.8 2.2-5 5-5s5 2.2 5 5-2.2 5-5 5"
        stroke="#FF7759"
        strokeWidth="1.8"
        strokeLinecap="round"
        fill="none"
      />
    </svg>
  ),
};

/**
 * Friendly label + colour tone for every provider. Used by the
 * provider cards and the model picker header so even unknown
 * providers get a sane fallback (a coloured initial chip).
 */
export const PROVIDER_META = {
  // international
  openai: { label: 'OpenAI', tone: 'bg-emerald-50 text-emerald-700' },
  anthropic: { label: 'Anthropic', tone: 'bg-orange-50 text-orange-700' },
  google: { label: 'Google Gemini', tone: 'bg-sky-50 text-sky-700' },
  deepseek: { label: 'DeepSeek', tone: 'bg-indigo-50 text-indigo-700' },
  moonshot: { label: 'Moonshot Kimi', tone: 'bg-violet-50 text-violet-700' },
  zhipu: { label: 'Zhipu GLM', tone: 'bg-rose-50 text-rose-600' },
  aliyun: { label: 'Aliyun Qwen', tone: 'bg-amber-50 text-amber-700' },
  doubao: { label: 'Doubao', tone: 'bg-blue-50 text-blue-700' },
  openrouter: { label: 'OpenRouter', tone: 'bg-pink-50 text-pink-700' },
  nvidia: { label: 'NVIDIA NIM', tone: 'bg-emerald-50 text-emerald-800' },
  minimax: { label: 'MiniMax', tone: 'bg-ink-900 text-white' },
  ollama: { label: 'Ollama', tone: 'bg-teal-50 text-teal-700' },
  vllm: { label: 'vLLM', tone: 'bg-cyan-50 text-cyan-700' },
  lmstudio: { label: 'LM Studio', tone: 'bg-purple-50 text-purple-700' },
  groq: { label: 'Groq', tone: 'bg-orange-50 text-orange-700' },
  mistral: { label: 'Mistral', tone: 'bg-yellow-50 text-yellow-800' },
  cohere: { label: 'Cohere', tone: 'bg-emerald-50 text-emerald-700' },
  kimi: { label: 'Kimi', tone: 'bg-neutral-100 text-neutral-800' },

  // Mainland China brands (romanized brand names)
  siliconflow: { label: 'SiliconFlow', tone: 'bg-indigo-50 text-indigo-700' },
  stepfun: { label: 'StepFun', tone: 'bg-sky-50 text-sky-700' },
  internlm: { label: 'InternLM', tone: 'bg-red-50 text-red-700' },
  mimo: { label: 'MiMo', tone: 'bg-orange-50 text-orange-700' },
  coze: { label: 'Coze', tone: 'bg-blue-50 text-blue-700' },
  baichuan: { label: 'Baichuan', tone: 'bg-cyan-50 text-cyan-700' },
  wenxin: { label: 'ERNIE Bot', tone: 'bg-blue-50 text-blue-800' },
  spark: { label: 'Spark', tone: 'bg-purple-50 text-purple-700' },
  hunyuan: { label: 'Hunyuan', tone: 'bg-teal-50 text-teal-700' },
  bailian: { label: 'Bailian', tone: 'bg-orange-50 text-orange-700' },
  zhinao: { label: '360 AI', tone: 'bg-green-50 text-green-700' },
  sensenova: { label: 'SenseNova', tone: 'bg-orange-50 text-orange-700' },
  yi: { label: 'Yi', tone: 'bg-sky-50 text-sky-700' },
};

export function providerLabel(p) {
  return PROVIDER_META[p]?.label || p;
}

export function providerTone(p) {
  return PROVIDER_META[p]?.tone || 'bg-ink-100 text-ink-700';
}

/**
 * Render a provider's brand mark.
 *
 *   <ProviderLogo provider="openai" size={20} />
 *
 * Resolution order:
 *   1. ``/providers/{id}.svg`` (real logo, brand-accurate)
 *   2. inline PATHS table (hand-drawn for non-simple-icons vendors)
 *   3. coloured initial chip (last-resort fallback)
 *
 * The remote logo is loaded with an ``onError`` handler so a
 * missing file falls back to the inline path silently. We don't
 * flicker the chip during the network round-trip because we
 * hide the ``<img>`` until the load event fires.
 */
export default function ProviderLogo({ provider, size = 20, className }) {
  const hasRemote = provider && REMOTE_LOGOS.has(provider);
  const hasInline = provider && PATHS[provider];
  const [remoteOk, setRemoteOk] = useState(hasRemote);

  // If the provider prop changes (e.g. dynamic lookup), reset the
  // remoteOk state so we re-attempt the load.
  useEffect(() => {
    setRemoteOk(hasRemote);
  }, [provider, hasRemote]);

  if (remoteOk && hasRemote) {
    return (
      <span
        className={cn('inline-flex shrink-0 items-center justify-center', className)}
        style={{ width: size, height: size }}
      >
        <img
          src={`/providers/${provider}.svg`}
          alt=""
          width={size}
          height={size}
          loading="lazy"
          style={{
            width: size,
            height: size,
            // Real brand SVGs use solid black/white fills by default.
            // CSS filter flips them in dark mode so they don't look
            // like a hole punched in the dark surface.
            filter: 'var(--provider-logo-filter, none)',
          }}
          onError={() => setRemoteOk(false)}
        />
      </span>
    );
  }

  if (hasInline) {
    return <span className={cn('inline-flex shrink-0', className)}>{PATHS[provider](size)}</span>;
  }

  const meta = PROVIDER_META[provider] || { label: provider, tone: 'bg-ink-100 text-ink-700' };
  const initial = (meta.label || provider || '?').slice(0, 1).toUpperCase();
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-md font-mono font-semibold',
        meta.tone,
        className,
      )}
      style={{ width: size, height: size, fontSize: Math.max(9, Math.round(size * 0.45)) }}
    >
      {initial}
    </span>
  );
}
