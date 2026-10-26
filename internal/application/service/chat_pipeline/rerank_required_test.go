package chatpipeline

import (
	"context"
	"errors"
	"testing"

	"github.com/Tencent/WeKnora/internal/models/rerank"
	"github.com/Tencent/WeKnora/internal/types"
	"github.com/Tencent/WeKnora/internal/types/interfaces"
)

type unavailableRequiredRerankService struct{ interfaces.ModelService }

func (*unavailableRequiredRerankService) GetRerankModel(context.Context, string) (rerank.Reranker, error) {
	return nil, errors.New("configured model was removed")
}

type failingRequiredReranker struct{ fixedReranker }

func (*failingRequiredReranker) Rerank(context.Context, string, []string) ([]rerank.RankResult, error) {
	return nil, errors.New("upstream rerank service unavailable")
}

func TestRequiredRerankStopsAnswerPipelineOnFailure(t *testing.T) {
	t.Setenv("WEKNORA_RERANK_REQUIRED", "true")
	for _, test := range []struct {
		name    string
		modelID string
		service interfaces.ModelService
		outcome types.RerankOutcome
	}{
		{"missing model", "", nil, types.RerankOutcomeNoModel},
		{"unavailable model", "rr-1", &unavailableRequiredRerankService{}, types.RerankOutcomeModelUnavailable},
		{"upstream failure", "rr-1", &rerankOnlyModelService{reranker: &failingRequiredReranker{}}, types.RerankOutcomeModelError},
	} {
		t.Run(test.name, func(t *testing.T) {
			cm := rerankChatManage(0.3)
			cm.RerankModelID = test.modelID
			called := false
			err := (&PluginRerank{modelService: test.service}).OnEvent(context.Background(), types.CHUNK_RERANK, cm, func() *PluginError {
				called = true
				return nil
			})
			if err == nil || err.ErrorType != ErrRerank.ErrorType || err.Err == nil || called {
				t.Fatalf("failed mandatory rerank must stop answer generation: error=%+v next=%v", err, called)
			}
			if cm.RerankDiagnostics == nil || cm.RerankDiagnostics.Outcome != test.outcome {
				t.Fatalf("failure diagnostics = %+v", cm.RerankDiagnostics)
			}
		})
	}
}

func TestRequiredRerankAllowsSuccessfulReranking(t *testing.T) {
	t.Setenv("WEKNORA_RERANK_REQUIRED", "true")
	cm := rerankChatManage(0.3)
	called := false
	plugin := &PluginRerank{modelService: &rerankOnlyModelService{reranker: &fixedReranker{scores: []float64{0.2, 0.8}}}}
	err := plugin.OnEvent(context.Background(), types.CHUNK_RERANK, cm, func() *PluginError { called = true; return nil })
	if err != nil || !called || len(cm.RerankResult) != 1 || cm.RerankResult[0].ID != "c2" {
		t.Fatalf("successful mandatory rerank should continue: error=%+v next=%v results=%+v", err, called, cm.RerankResult)
	}
}

func TestOptionalRerankRetainsExistingFallback(t *testing.T) {
	t.Setenv("WEKNORA_RERANK_REQUIRED", "")
	cm := rerankChatManage(0.3)
	called := false
	plugin := &PluginRerank{modelService: &unavailableRequiredRerankService{}}
	err := plugin.OnEvent(context.Background(), types.CHUNK_RERANK, cm, func() *PluginError { called = true; return nil })
	if err != nil || !called || cm.RerankDiagnostics.Outcome != types.RerankOutcomeModelUnavailable {
		t.Fatalf("default fallback changed: error=%+v next=%v", err, called)
	}
}
