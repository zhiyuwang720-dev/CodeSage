#!/usr/bin/env bb

;; Clone a repo from GitHub org, checkout oldest PR branch, and open interactive Claude Code
;; 
;; Usage: bb clone-and-review.bb <org>/<repo>
;; 
;; Examples:
;;   bb clone-and-review.bb my-org/my-repo
;;   bb clone-and-review.bb code-review-benchmark/sentry__sentry__claude__PR95633__20260127
;;
;; Requirements:
;;   - GitHub CLI (gh) installed and authenticated
;;   - Claude Code CLI installed

(require '[babashka.process :refer [shell process check exec]]
         '[clojure.string :as str]
         '[cheshire.core :as json]
         '[babashka.fs :as fs])

;; ============================================================================
;; Argument Parsing
;; ============================================================================

(defn parse-args [args]
  (let [args-vec (vec args)]
    (when (or (empty? args-vec)
              (not (str/includes? (first args-vec) "/")))
      (println "Usage: bb clone-and-review.bb <org>/<repo>")
      (println "")
      (println "Examples:")
      (println "  bb clone-and-review.bb my-org/my-repo")
      (println "  bb clone-and-review.bb code-review-benchmark/sentry__sentry__claude__PR95633__20260127")
      (System/exit 1))
    
    (let [full-name (first args-vec)
          [org repo] (str/split full-name #"/" 2)]
      {:org org
       :repo repo
       :full-name full-name})))

;; ============================================================================
;; GitHub CLI Helpers
;; ============================================================================

(defn run-gh [& args]
  "Run a GitHub CLI command and return the output"
  (let [result (apply shell {:out :string :err :string :continue true} "gh" args)]
    (if (zero? (:exit result))
      (:out result)
      (do
        (println "Error running gh command:" (str/join " " args))
        (println "stderr:" (:err result))
        nil))))

(defn clone-repo [full-name target-dir repo-name]
  "Clone a repository to the target directory"
  (let [repo-path (str target-dir "/" repo-name)]
    (if (fs/exists? repo-path)
      (do
        (println (str "⏭️  Skipping clone, " repo-name " already exists"))
        repo-path)
      (do
        (println (str "📥 Cloning " full-name "..."))
        (let [result (shell {:out :string :err :string :continue true :dir target-dir}
                           "gh" "repo" "clone" full-name)]
          (if (zero? (:exit result))
            (do
              (println (str "✅ Cloned " repo-name))
              repo-path)
            (do
              (println (str "❌ Failed to clone: " (:err result)))
              (System/exit 1))))))))

(defn list-open-prs [full-name]
  "List all open PRs for a repository, sorted by creation date (oldest first)"
  (println "📋 Fetching open PRs...")
  (let [output (run-gh "pr" "list" 
                       "--repo" full-name
                       "--state" "open"
                       "--json" "number,title,headRefName,createdAt"
                       "--limit" "100")]
    (when output
      (let [prs (json/parse-string output true)]
        (sort-by :createdAt prs)))))

(defn checkout-pr-branch [repo-path branch-name]
  "Checkout the PR branch in the repository"
  (println (str "🔀 Checking out branch: " branch-name))
  (let [fetch-result (shell {:out :string :err :string :continue true :dir repo-path}
                           "git" "fetch" "origin" branch-name)]
    (if (zero? (:exit fetch-result))
      (let [checkout-result (shell {:out :string :err :string :continue true :dir repo-path}
                                  "git" "checkout" branch-name)]
        (if (zero? (:exit checkout-result))
          (do
            (println (str "✅ Checked out " branch-name))
            true)
          (let [track-result (shell {:out :string :err :string :continue true :dir repo-path}
                                   "git" "checkout" "-b" branch-name (str "origin/" branch-name))]
            (if (zero? (:exit track-result))
              (do
                (println (str "✅ Checked out " branch-name " (with tracking)"))
                true)
              (do
                (println (str "❌ Failed to checkout " branch-name))
                (System/exit 1))))))
      (do
        (println (str "❌ Failed to fetch branch " branch-name))
        (System/exit 1)))))

(defn open-claude-code [repo-path]
  "Open interactive Claude Code session in the repo directory"
  (println "")
  (println (str "🤖 Opening Claude Code in " repo-path "..."))
  (println "   Run: /code-review:code-review --comment")
  (println "")
  ;; Use ProcessBuilder to set directory and inherit IO
  (let [pb (ProcessBuilder. ["claude"])]
    (.directory pb (java.io.File. repo-path))
    (.inheritIO pb)
    (-> pb .start .waitFor)
    (System/exit 0)))

;; ============================================================================
;; Main
;; ============================================================================

(defn main [args]
  (let [{:keys [org repo full-name]} (parse-args args)
        working-dir (System/getProperty "user.dir")]
    
    (println "")
    (println "🚀 GitHub PR Reviewer")
    (println "=====================")
    (println (str "Repository: " full-name))
    (println (str "Working directory: " working-dir))
    (println "")
    
    ;; Check if gh CLI is available
    (let [gh-check (shell {:out :string :err :string :continue true} "gh" "--version")]
      (when-not (zero? (:exit gh-check))
        (println "❌ GitHub CLI (gh) is not installed or not in PATH")
        (System/exit 1)))
    
    ;; Clone repo
    (let [repo-path (clone-repo full-name working-dir repo)]
      
      ;; Find and checkout oldest PR
      (if-let [prs (list-open-prs full-name)]
        (if (seq prs)
          (let [oldest-pr (first prs)
                branch (:headRefName oldest-pr)
                pr-number (:number oldest-pr)
                pr-title (:title oldest-pr)]
            (println (str "📌 Found " (count prs) " open PR(s). Oldest: #" pr-number " - " pr-title))
            (checkout-pr-branch repo-path branch)
            (open-claude-code repo-path))
          (do
            (println "ℹ️  No open PRs found")
            (System/exit 0)))
        (do
          (println "❌ Failed to fetch PRs")
          (System/exit 1))))))

;; Run main
(main *command-line-args*)
