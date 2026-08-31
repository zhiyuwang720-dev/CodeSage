export type GitHubRepositoryTarget = 'security' | 'issues' | 'pulls';

export interface GitHubRepository {
  owner: string;
  repo: string;
}

function stripGitSuffix(value: string) {
  return value.replace(/\.git$/i, '');
}

export function parseGitHubRepository(repositoryUrl?: string | null): GitHubRepository | null {
  const value = repositoryUrl?.trim();
  if (!value) return null;

  const sshMatch = value.match(/^git@github\.com:([^/\s]+)\/([^/\s#?]+?)(?:\.git)?$/i);
  if (sshMatch) {
    return {
      owner: sshMatch[1],
      repo: stripGitSuffix(sshMatch[2]),
    };
  }

  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    if (host !== 'github.com' && host !== 'www.github.com') return null;

    const [owner, repo] = url.pathname.split('/').filter(Boolean);
    if (!owner || !repo) return null;

    return {
      owner,
      repo: stripGitSuffix(repo),
    };
  } catch {
    return null;
  }
}

export function buildGitHubRepositoryTarget(
  repositoryUrl: string | null | undefined,
  target: GitHubRepositoryTarget
) {
  const repository = parseGitHubRepository(repositoryUrl);
  if (!repository) return null;

  return `https://github.com/${repository.owner}/${repository.repo}/${target}`;
}
