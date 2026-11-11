// Only explicitly configured environment bindings enter the private model configuration.
export function resolveConfiguration(input, environment = process.env) {
  const config = structuredClone(input);
  for (const channel of config.models.channels) {
    const match = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(channel.baseUrl);
    if (match) {
      if (!environment[match[1]]) throw new Error(`Missing endpoint environment variable: ${match[1]}`);
      channel.baseUrl = environment[match[1]];
    }
  }
  return config;
}

export function privateValueRedactor(config, environment = process.env) {
  const values = new Set();
  for (const channel of config.models.channels) {
    values.add(channel.baseUrl);
    values.add(new URL(channel.baseUrl).origin);
    if (channel.authentication.kind === 'environment') {
      const value = environment[channel.authentication.variable];
      if (value) values.add(value);
    }
    for (const value of Object.values(channel.connection?.headers ?? {})) values.add(value);
  }
  const strings = [...values].filter(Boolean).sort((a, b) => b.length - a.length);
  const text = value => strings.reduce((result, secret) => result.split(secret).join('[private]'), value);
  const redact = value => {
    if (typeof value === 'string') return text(value);
    if (Array.isArray(value)) return value.map(redact);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) => [text(key), redact(item)]));
    return value;
  };
  return redact;
}
