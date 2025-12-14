"use client";

import React, { useState, useEffect } from 'react';
import { useChat } from 'ai/react';
import { ChatInput, ChatMessages, type Message } from './ui/chat';

export default function ChatSection() {
  const {
    messages,
    input,
    isLoading,
    handleSubmit,
    handleInputChange,
    reload,
    stop,
  } = useChat({ api: process.env.NEXT_PUBLIC_CHAT_API });

  // Define a default message
  const defaultMessage: Message = {
    id: 'default-msg-0',
    content: 'Welcome to rUv Bot! I’m here to provide insights about Reuven Cohen, embodying the groove of innovation.\n' +
             'Instagram: https://www.instagram.com/ruv\n' +
             'LinkedIn: https://www.linkedin.com/in/reuvencohen/\n' +
             'GitHub: https://github.com/ruvnet/\n' +
             'Feel free to ask me a question about rUv!',
    role: 'system',
  };


  // State to manage the array of messages, initialized with the default message
  const [chatMessages, setChatMessages] = useState<Message[]>([defaultMessage]);

  // Effect to update the chatMessages state with new messages from useChat
  useEffect(() => {
    if (messages && messages.length > 0) {
      setChatMessages(messages as Message[]);
    }
  }, [messages]);

  return (
    <div className="space-y-4 max-w-5xl w-full">
      <ChatMessages
        messages={chatMessages}
        isLoading={isLoading}
        reload={reload}
        stop={stop}
      />
      <ChatInput
        input={input}
        handleSubmit={handleSubmit}
        handleInputChange={handleInputChange}
        isLoading={isLoading}
      />
    </div>
  );
}