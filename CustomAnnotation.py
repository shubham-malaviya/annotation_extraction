from pdfannots.types import Annotation
import typing as typ

class CustomAnnotation(Annotation):
    """Extension of Annotation with context_sentence support."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.context_sentence: typ.Optional[str] = None
    
    
    def set_context_sentence(self, page_text: str) -> None:
        """
        set the context sentence for this annotation.
        For example, you might want to extract the full sentence from the page text that contains the annotation's captured text.

        """
        import re
        if not getattr(self, "text", None):
            return
        sentences = list(map(lambda x: str(x).replace("-\n", "-").replace("\n", " "), re.split(r'(?<=[.!?])\s+', page_text)))  # this works at some extent, could be improved.

        # Identy annotation text spanning multiple sentences
        annotation_text = self.gettext().strip()
        annotation_sentences = list(map(lambda x: x.strip(), re.split(r'(?<=[.!?])\s+', annotation_text)))
    
        text_to_add =[]
        for i in range(0, len(sentences)):
                # We first split sentences. Now when annotation span multiple sentences, single sentence based matching will fail and sentence_context is none
                # To handle such scenario, we will split annotation_text into multiple sentences and then loop over them to identify all sentences.

                if annotation_sentences[0] in sentences[i].strip():
                    k= i
                    text_to_add.append(sentences[i].strip())
                    for j in range(1,len(annotation_sentences)):
                        if annotation_sentences[j] in sentences[k+1].strip():
                            text_to_add.append(sentences[k+1].strip())
                            k+=1
                    self.context_sentence = " ".join(text_to_add)
                    return