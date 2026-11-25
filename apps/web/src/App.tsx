import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { CareersPage } from "@/pages/CareersPage";
import { CompaniesPage } from "@/pages/CompaniesPage";
import { FindJobsPage } from "@/pages/FindJobsPage";
import { JobsPage } from "@/pages/JobsPage";
import { MatchesPage } from "@/pages/MatchesPage";
import { ResultsPage } from "@/pages/ResultsPage";
import { ReviewPage } from "@/pages/ReviewPage";
import { RunResultPage } from "@/pages/RunResultPage";
import { RunsPage } from "@/pages/RunsPage";
import { StartMatchPage } from "@/pages/StartMatchPage";
import { WelcomePage } from "@/pages/WelcomePage";
import { ProfileWizardPage } from "@/pages/wizard/ProfileWizardPage";
import { AppProviders } from "@/providers";

/**
 * Route map. The old URLs (/, /review, /sources, /wait/:runId, /wizard/profile)
 * are kept as redirects so a bookmarked or shared link still lands somewhere
 * sensible after the navigation restructure.
 */
export function App() {
  return (
    <AppProviders>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<WelcomePage />} />
            <Route path="/profile" element={<ProfileWizardPage />} />

            <Route path="/match" element={<StartMatchPage />} />
            <Route path="/matches" element={<MatchesPage />} />

            <Route path="/search/careers" element={<CareersPage />} />
            <Route path="/search/review" element={<ReviewPage />} />
            <Route path="/search/jobs" element={<FindJobsPage />} />
            <Route path="/search/results" element={<ResultsPage />} />
            <Route path="/search/results/:runId" element={<RunResultPage />} />

            <Route path="/companies" element={<CompaniesPage />} />
            <Route path="/jobs" element={<JobsPage />} />

            <Route path="/runs" element={<RunsPage />} />

            {/* Pre-restructure paths. */}
            <Route path="/wizard/profile" element={<Navigate to="/profile" replace />} />
            <Route path="/review" element={<Navigate to="/search/review" replace />} />
            <Route path="/sources" element={<Navigate to="/search/careers" replace />} />
            <Route path="/wait" element={<Navigate to="/search/results" replace />} />
            <Route
              path="/wait/:runId"
              element={<LegacyRunRedirect />}
            />
          </Route>
        </Routes>
      </BrowserRouter>
    </AppProviders>
  );
}

/** `/wait/12` -> `/search/results/12`. */
function LegacyRunRedirect() {
  const { runId } = useParams();
  return <Navigate to={`/search/results/${runId ?? ""}`} replace />;
}
