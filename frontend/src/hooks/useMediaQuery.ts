import { useCallback, useEffect, useState } from "react";

export function useMediaQuery(query: string) {
  const getMatches = useCallback(() => window.matchMedia(query).matches, [query]);
  const [matches, setMatches] = useState(getMatches);

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);

  return matches;
}
