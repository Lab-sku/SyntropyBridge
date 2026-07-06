export default function TypingIndicator() {
  return (
    <div className="flex items-center gap-1 px-1 py-1">
      <div
        className="h-1.5 w-1.5 rounded-full bg-ink-400"
        style={{ animation: 'typing-dot 1.2s ease-in-out infinite', animationDelay: '0ms' }}
      />
      <div
        className="h-1.5 w-1.5 rounded-full bg-ink-400"
        style={{ animation: 'typing-dot 1.2s ease-in-out infinite', animationDelay: '200ms' }}
      />
      <div
        className="h-1.5 w-1.5 rounded-full bg-ink-400"
        style={{ animation: 'typing-dot 1.2s ease-in-out infinite', animationDelay: '400ms' }}
      />
    </div>
  );
}
