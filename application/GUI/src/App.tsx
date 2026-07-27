import { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import "./App.css";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Avatar,
  AvatarFallback,
  AvatarImage,
} from "@/components/ui/avatar";
import { Card, CardContent } from "@/components/ui/card";
import { Trash2 } from "lucide-react";

import cisucLogoAvatar from "./assets/cisuc_logo.svg";

import type { Message } from "./types/chat";

const generateId = (): string => {
  return `${Date.now()}-${Math.random()
    .toString(36)
    .substring(2, 9)}`;
};

const INFO_MESSAGE: Message = {
  id: generateId(), // Use the new function
  text: `
Hello! I’m the CISUC ChatBot!
I was created to help you learn more about the Centre for Informatics and Systems of the University of Coimbra.
You can ask me questions about CISUC, its research areas, groups and projects, researchers, publications, events, and other scientific activities.
I’m still a beta version, so please confirm important information on the official CISUC website: https://www.cisuc.uc.pt/.`,
  sender: "assistant",
};

function App() {
  const [messages, setMessages] = useState<Message[]>([INFO_MESSAGE]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [streamingMessageId, setStreamingMessageId] = useState<string | null>(
    null
  );

  const messagesEndRef = useRef<HTMLDivElement>(null);
  
  const handleSend = async () => {
    const trimmedInput = input.trim();

    if (!trimmedInput || isLoading) {
      return;
    }

    setIsLoading(true);

    const userMessage: Message = {
      id: generateId(),
      text: trimmedInput,
      sender: "user",
    };

    const assistantMessageId = generateId();
    setStreamingMessageId(assistantMessageId);

    const assistantMessage: Message = {
      id: assistantMessageId,
      text: "",
      sender: "assistant",
    };

    setMessages((previousMessages) => [
      ...previousMessages,
      userMessage,
      assistantMessage,
    ]);

    setInput("");

    try {
      const orchestratorEndpoint =
        import.meta.env.VITE_ORCHESTRATOR_API_ENDPOINT;

      console.log("Calling orchestrator:", orchestratorEndpoint);
      console.log("Request payload:", {
        pergunta: trimmedInput,
      });

      const response = await fetch(orchestratorEndpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/plain",
        },
        body: JSON.stringify({
          pergunta: trimmedInput,
        }),
      });

      console.log("Response status:", response.status);
      console.log("Response status text:", response.statusText);
      console.log(
        "Response content type:",
        response.headers.get("content-type")
      );

      if (!response.ok) {
        const errorBody = await response.text();

        console.error("Orchestrator error response:", {
          status: response.status,
          statusText: response.statusText,
          body: errorBody,
        });

        throw new Error(
          `HTTP ${response.status} ${response.statusText}: ${errorBody}`
        );
      }

      if (!response.body) {
        throw new Error("The response body is empty.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");

      let streamedText = "";
      let animationFrameId: number | null = null;

      const updateDisplayedText = () => {
        setMessages((previousMessages) =>
          previousMessages.map((message) =>
            message.id === assistantMessageId
              ? {
                  ...message,
                  text: streamedText,
                }
              : message
          )
        );

        animationFrameId = null;
      };

      while (true) {
        const { value, done } = await reader.read();

        if (done) {
          streamedText += decoder.decode();
          break;
        }

        const chunk = decoder.decode(value, {
          stream: true,
        });

        console.debug("Received stream chunk:", chunk);

        streamedText += chunk;

        // Limit UI updates to approximately the browser refresh rate.
        if (animationFrameId === null) {
          animationFrameId = requestAnimationFrame(updateDisplayedText);
        }
      }

      if (animationFrameId !== null) {
        cancelAnimationFrame(animationFrameId);
      }

      // Display the complete final response.
      setMessages((previousMessages) =>
        previousMessages.map((message) =>
          message.id === assistantMessageId
            ? {
                ...message,
                text: streamedText,
              }
            : message
        )
      );

      setStreamingMessageId(null);

    } catch (error) {
      console.error("Full orchestrator request error:", error);
      setStreamingMessageId(null);

      let errorMessage = "Unknown error";

      if (error instanceof Error) {
        errorMessage = error.message;

        console.error("Error name:", error.name);
        console.error("Error message:", error.message);
        console.error("Error stack:", error.stack);
      }

      setMessages((previousMessages) =>
        previousMessages.map((message) =>
          message.id === assistantMessageId
            ? {
                ...message,
                text: `Sorry, I encountered an error: ${errorMessage}`,
              }
            : message
        )
      );
    } finally {
      setIsLoading(false);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Prevent sending if loading or shift key is pressed
    if (e.key === "Enter" && !e.shiftKey && !isLoading) {
      e.preventDefault();
      handleSend();
    }
  };

  // Function to reset the chat
  const handleReset = () => {
    setMessages([INFO_MESSAGE]);
    setStreamingMessageId(null);
  };

  // Effect to scroll down when messages update
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]); // Dependency: run when messages change

  return (
    <div className="flex flex-col h-screen bg-background">
      <header className="p-4 shrink-0 border-b flex items-center justify-between gap-3">
        {/*img src={deiLogoFull} alt="DEI Logo" className="h-10 w-auto" />*/}
        <div className="w-10" />{" "}
        <h1 className="text-xl font-semibold">CISUC ChatBot</h1>
        <Button
          variant="ghost"
          size="icon"
          onClick={handleReset}
          aria-label="Reset Chat"
        >
          <Trash2 className="h-10" />
        </Button>
      </header>
      <div className="flex-1 flex flex-col max-w-3xl w-full mx-auto my-8 overflow-hidden rounded-lg border bg-card shadow-lg">
        <div className="flex-1 overflow-hidden">
          <ScrollArea className="h-full p-4">
            <div className="space-y-4">
              {messages
                .filter((msg) => msg.sender !== "system")
                .map((message) => (
                  <div
                    key={message.id}
                    className={`flex items-end gap-2 ${
                      message.sender === "user"
                        ? "justify-end"
                        : "justify-start"
                    }`}
                  >
                    {message.sender === "assistant" && (
                      <Avatar className="h-10 w-10 border">
                        <AvatarImage
                          src={cisucLogoAvatar}
                          alt="CISUC ChatBot"
                          className="scale-80"
                        />
                        <AvatarFallback>CISUC</AvatarFallback>
                      </Avatar>
                    )}
                    <Card
                      className={`max-w-xs md:max-w-md lg:max-w-lg ${
                        message.sender === "user"
                          ? "bg-primary text-primary-foreground"
                          : "bg-muted"
                      }`}
                    >
                      <CardContent className="px-5 break-words">
                        {message.id === streamingMessageId ? (
                          <div className="whitespace-pre-wrap">
                            {message.text || "..."}
                          </div>
                        ) : (
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {message.text}
                          </ReactMarkdown>
                        )}
                      </CardContent>
                    </Card>
                    {message.sender === "user" && (
                      <Avatar className="h-10 w-10 border">
                        <AvatarFallback>U</AvatarFallback>
                      </Avatar>
                    )}
                  </div>
                ))}
              {/* Empty div at the end of messages to target for scrolling */}
              <div ref={messagesEndRef} />
            </div>
          </ScrollArea>
        </div>
        {/* Existing chat input footer, now inside the box */}
        <footer className="p-4 border-t shrink-0">
          {/* Removed max-w-3xl and mx-auto from this div */}
          <div className="flex gap-2 items-center">
            <Textarea
              value={input}
              onChange={handleInputChange}
              onKeyDown={handleKeyDown}
              placeholder={
                isLoading
                  ? "Generating response..."
                  : "Type your message... (Shift + Enter for new line)"
              }
              className="flex-1 resize-none"
              maxLength={300}
              disabled={isLoading}
            />
            <Button onClick={handleSend} disabled={isLoading}>
              {isLoading ? "..." : "Send"}
            </Button>
          </div>
        </footer>
      </div>{" "}
      {/* End of new box wrapper */}
      <footer className="text-center p-4 text-xs text-muted-foreground border-t shrink-0">
        <p>
          © 2026 CISUC ChatBot. For demonstration purposes only. Verify critical
          information.
        </p>
        <p>
          Created by Arthur Sophiatti, Nuno Lourenço, and João R
          Campos.
        </p>
      </footer>
    </div>
  );
}

export default App;
