import { useState, useCallback } from "react";
import type { Project } from "@/shared/types";
import { api } from "@/shared/config/database";
import { toast } from "sonner";

export function useProjects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);

  const loadProjects = useCallback(async () => {
    try {
      setLoading(true);
      const data = await api.getProjects();
      setProjects(data.filter((p) => p.is_active));
    } catch (error) {
      console.error("Failed to load projects:", error);
      toast.error("加载项目失败");
    } finally {
      setLoading(false);
    }
  }, []);

  return { projects, loading, loadProjects };
}
